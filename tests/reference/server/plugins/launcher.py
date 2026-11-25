import json
import logging
import os
import queue
import re
import secrets
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from server.platform import runtime
from server.plugins import client, contract, inventory, paths
from server.plugins.errors import PluginError, PluginUnavailable
from server.plugins.registry import Entry

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[\x21-\x7e]{1,4096}")
ENVIRONMENT_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
MAX_LINE_BYTES = 64 * 1024


@dataclass(frozen=True)
class StartLimits:
    ready_seconds: float = contract.SHORT_SECONDS
    stop_grace_seconds: float = 5.0


@dataclass
class Running:
    entry: Entry
    process: subprocess.Popen | None
    endpoint: client.Endpoint
    manifest: dict

    def alive(self) -> bool:
        return self.process is None or self.process.poll() is None


class StartFailure(PluginError):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(code, message or code)
        self.retryable = retryable


def _plugin_environment() -> dict[str, str]:
    return {key: os.environ[key] for key in ENVIRONMENT_ALLOWLIST if key in os.environ}


def _grants(entry: Entry, data_dir: str) -> dict[str, str]:
    data_root = Path(data_dir).expanduser().resolve()
    grants = {}
    for grant in entry.grants:
        if grant == "plugin_cache":
            grants[grant] = str(paths.cache_dir(entry.id))
        elif grant == "tax_dir":
            grants[grant] = str(paths.tax_dir(data_root, entry.binding["territory"]))
    return grants


def _secrets(entry: Entry, data_dir: str) -> dict[str, str]:
    stored: dict = {}
    secrets_file = Path(data_dir) / ".secrets.json"
    if entry.credentials and secrets_file.exists():
        try:
            stored = runtime._read_json_object(secrets_file, "Secrets file")
        except (RuntimeError, ValueError) as exc:
            logger.warning("Plug-in %s secrets cannot be read: %s", entry.id, type(exc).__name__)
            raise StartFailure("unavailable") from exc
    resolved = {}
    for name, spec in entry.credentials.items():
        value = os.environ.get(spec["env"], "").strip() if spec["env"] else ""
        if not value:
            candidate = stored.get(spec["secret"], "")
            value = candidate.strip() if isinstance(candidate, str) else ""
        if value:
            resolved[name] = value
    return resolved


def stop_process(process: subprocess.Popen, grace_seconds: float) -> None:
    if process.poll() is None and process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    for step in (None, process.terminate, process.kill):
        if step is not None and process.poll() is None:
            step()
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            continue
    process.wait()


def _pump_stdout(process: subprocess.Popen, lines: "queue.Queue[bytes | None]", plugin_id: str) -> None:
    first = True
    assert process.stdout is not None
    for line in iter(lambda: process.stdout.readline(MAX_LINE_BYTES), b""):
        if first:
            lines.put(line)
            first = False
        else:
            logger.warning("Plug-in %s wrote to stdout after its ready line; stdout carries only that line", plugin_id)
    if first:
        lines.put(None)


def _pump_stderr(process: subprocess.Popen, plugin_id: str) -> None:
    assert process.stderr is not None
    for line in iter(lambda: process.stderr.readline(MAX_LINE_BYTES), b""):
        logger.info("plug-in %s: %s", plugin_id, line.decode("utf-8", "replace").rstrip()[:2000])


def check_installation(entry: Entry) -> None:
    if not entry.confirmed:
        raise StartFailure("unconfirmed")
    if not contract.contract_matches(entry.family, entry.contract_revision):
        raise StartFailure("incompatible_contract")
    if entry.command is None:
        return
    root = paths.version_dir(entry.id, entry.version)
    executable = Path(entry.command[0])
    if ".." in executable.parts or not Path(os.path.realpath(executable)).is_relative_to(os.path.realpath(root)):
        logger.warning("Plug-in %s command is outside its version directory", entry.id)
        raise StartFailure("inventory_mismatch")
    if not root.is_dir():
        raise StartFailure("missing_executable")
    changed = inventory.differences(root, entry.inventory_sha256 or "")
    if changed:
        logger.warning("Plug-in %s installed files differ from the inventory: %s", entry.id, ", ".join(changed[:20]))
        raise StartFailure("inventory_mismatch")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise StartFailure("missing_executable")


def _read_token_file(entry: Entry) -> str:
    try:
        token = Path(entry.token_file or "").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Plug-in %s token file cannot be read: %s", entry.id, type(exc).__name__)
        raise StartFailure("unavailable") from exc
    token = token[:-1] if token.endswith("\n") else token
    if _TOKEN.fullmatch(token) is None:
        logger.warning("Plug-in %s token file holds no valid token", entry.id)
        raise StartFailure("unavailable")
    return token


def _verify_manifest(entry: Entry, endpoint: client.Endpoint) -> dict:
    try:
        manifest = client.request_json(endpoint, "GET", "/v1/manifest", None, timeout=contract.SHORT_SECONDS)
    except PluginError as exc:
        raise StartFailure("crashed", "manifest request failed", retryable=True) from exc
    if not isinstance(manifest, dict):
        raise StartFailure("identity_mismatch", "manifest is not an object")
    for key in ("id", "family", "version"):
        if manifest.get(key) != getattr(entry, key):
            raise StartFailure("identity_mismatch", f"manifest {key} differs from the registry entry")
    for key in ("contract_version", "contract_revision"):
        if manifest.get(key) != getattr(entry, key):
            raise StartFailure("incompatible_contract", f"manifest {key} differs from the registry entry")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != {"operations", "max_concurrency"}:
        raise StartFailure("incompatible_contract", "manifest capabilities are invalid")
    operations, max_concurrency = capabilities["operations"], capabilities["max_concurrency"]
    known = contract.OPERATIONS[entry.family]
    if (
        not isinstance(operations, list)
        or not all(isinstance(op, str) and op in known for op in operations)
        or len(set(operations)) != len(operations)
    ):
        raise StartFailure("incompatible_contract", "manifest operations are invalid")
    if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) or max_concurrency < 1:
        raise StartFailure("incompatible_contract", "manifest max_concurrency is invalid")
    try:
        health = client.request_json(endpoint, "GET", "/v1/health", None, timeout=contract.SHORT_SECONDS)
    except PluginError as exc:
        raise StartFailure("crashed", "health request failed", retryable=True) from exc
    status = health.get("status") if isinstance(health, dict) else None
    if not isinstance(status, str) or status not in {"ok", "degraded"}:
        raise StartFailure("crashed", "health response is invalid", retryable=True)
    return manifest


def start(entry: Entry, *, data_dir: str, locale: str, limits: StartLimits) -> Running:
    check_installation(entry)
    if entry.url is not None:
        endpoint = client.Endpoint(base_url=entry.url.rstrip("/"), token=_read_token_file(entry))
        return Running(entry=entry, process=None, endpoint=endpoint, manifest=_verify_manifest(entry, endpoint))
    token = secrets.token_hex(32)
    handshake = {
        "protocol_version": contract.PROTOCOL_VERSION,
        "token": token,
        "grants": _grants(entry, data_dir),
        "secrets": _secrets(entry, data_dir),
        "locale": locale,
        "lock_path": str(paths.lock_path(entry.id)),
    }
    try:
        process = subprocess.Popen(
            list(entry.command or ()),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_plugin_environment(),
            cwd=paths.version_dir(entry.id, entry.version),
            close_fds=True,
        )
    except OSError as exc:
        raise StartFailure("missing_executable") from exc
    try:
        return _connect(entry, process, token, handshake, limits)
    except PluginError:
        stop_process(process, limits.stop_grace_seconds)
        raise
    except Exception as exc:
        stop_process(process, limits.stop_grace_seconds)
        logger.warning("Plug-in %s start failed: %s", entry.id, type(exc).__name__)
        raise StartFailure("crashed", "the start failed") from exc


def _connect(entry: Entry, process: subprocess.Popen, token: str, handshake: dict, limits: StartLimits) -> Running:
    lines: queue.Queue[bytes | None] = queue.Queue()
    threading.Thread(target=_pump_stdout, args=(process, lines, entry.id), daemon=True).start()
    threading.Thread(target=_pump_stderr, args=(process, entry.id), daemon=True).start()
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps(handshake, separators=(",", ":")).encode("utf-8") + b"\n")
        process.stdin.flush()
    except OSError as exc:
        raise StartFailure("crashed", "plug-in closed stdin before the handshake", retryable=True) from exc
    try:
        line = lines.get(timeout=limits.ready_seconds)
    except queue.Empty:
        raise StartFailure("crashed", "no ready line", retryable=True) from None
    try:
        ready = json.loads(line) if line is not None else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        ready = None
    if isinstance(ready, dict) and ready == {"error": "lock_held"}:
        try:
            process.wait(timeout=limits.ready_seconds)
        except subprocess.TimeoutExpired:
            pass
        raise PluginUnavailable("busy")
    port = ready.get("port") if isinstance(ready, dict) and set(ready) == {"port"} else None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise StartFailure("crashed", "invalid ready line", retryable=True)
    endpoint = client.Endpoint(base_url=f"http://127.0.0.1:{port}", token=token)
    return Running(entry=entry, process=process, endpoint=endpoint, manifest=_verify_manifest(entry, endpoint))
