"""Process runtime of the plug-in, as `contracts/plugin-runtime/v1` defines it."""

import fcntl
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO

from meridian_provider_sharadar import operations
from meridian_provider_sharadar.operations import Context, PluginIdentity, Problem

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
FAMILY = "market-data"
MAX_HANDSHAKE_BYTES = 64 * 1024
MAX_BODY_BYTES = 1024 * 1024
NASDAQ_API_BASE = "https://data.nasdaq.com/api/v3"
HANDSHAKE_KEYS = frozenset({"protocol_version", "token", "grants", "secrets", "locale", "lock_path"})
LOCALES = frozenset({"en", "pt-BR"})
AMBIENT_NASDAQ_SETTINGS = ("NASDAQ_DATA_LINK_API_KEY", "NASDAQ_DATA_LINK_BASE_DOMAIN")
_TOKEN = re.compile(r"[0-9a-f]{64}", re.ASCII)


class StartError(Exception):
    """The plug-in cannot start with this handshake or plugin.json."""


def load_identity(plugin_json: Path) -> PluginIdentity:
    """Read the identity of the plug-in from ``plugin.json`` of its version directory."""
    try:
        document = json.loads(plugin_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StartError("plugin.json cannot be read") from exc
    if not isinstance(document, dict) or document.get("family") != FAMILY:
        raise StartError("plugin.json is not a market-data plug-in")
    values = [document.get(key) for key in ("id", "version", "contract_version", "contract_revision")]
    if not all(isinstance(value, str) and value for value in values):
        raise StartError("plugin.json has no valid identity")
    return PluginIdentity(*values)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def loads_strict(data: bytes) -> object:
    """Parse JSON and reject ``NaN``, ``Infinity`` and numbers out of the float range."""
    return json.loads(data.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)


def read_handshake(stdin: BinaryIO) -> dict:
    """Read and check the one handshake line of the host."""
    line = stdin.readline(MAX_HANDSHAKE_BYTES + 1)
    if len(line) > MAX_HANDSHAKE_BYTES or not line.endswith(b"\n"):
        raise StartError("the handshake line is missing or too long")
    try:
        handshake = loads_strict(line)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise StartError("the handshake line is not JSON") from exc
    if not isinstance(handshake, dict) or set(handshake) != HANDSHAKE_KEYS:
        raise StartError("the handshake keys differ from the contract")
    if handshake["protocol_version"] != PROTOCOL_VERSION or isinstance(handshake["protocol_version"], bool):
        raise StartError("the handshake protocol version is not supported")
    if not isinstance(handshake["token"], str) or _TOKEN.fullmatch(handshake["token"]) is None:
        raise StartError("the handshake token is invalid")
    grants, keys = handshake["grants"], handshake["secrets"]
    if not isinstance(grants, dict) or set(grants) != {"plugin_cache"} or not isinstance(grants["plugin_cache"], str):
        raise StartError("the handshake grants differ from plugin.json")
    if not Path(grants["plugin_cache"]).is_absolute():
        raise StartError("the plugin_cache grant is not an absolute path")
    if not isinstance(keys, dict) or not set(keys) <= {operations.CREDENTIAL_NAME}:
        raise StartError("the handshake secrets differ from plugin.json")
    if not all(isinstance(value, str) for value in keys.values()):
        raise StartError("a handshake secret is not a string")
    if not isinstance(handshake["locale"], str) or handshake["locale"] not in LOCALES:
        raise StartError("the handshake locale is not supported")
    if not isinstance(handshake["lock_path"], str) or not Path(handshake["lock_path"]).is_absolute():
        raise StartError("the handshake lock_path is not an absolute path")
    return handshake


def configure_nasdaq_client() -> None:
    """Import Nasdaq Data Link without its ambient key and endpoint settings, and pin the endpoint.

    The client reads ``NASDAQ_DATA_LINK_API_KEY``, ``NASDAQ_DATA_LINK_BASE_DOMAIN`` and
    ``~/.nasdaq/data_link_apikey`` when it is imported. The key comes only from the handshake.
    """
    for name in AMBIENT_NASDAQ_SETTINGS:
        os.environ.pop(name, None)
    home = os.environ.get("HOME")
    os.environ["HOME"] = os.devnull
    try:
        import nasdaqdatalink
    finally:
        if home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = home
    nasdaqdatalink.ApiConfig.api_key = None
    nasdaqdatalink.ApiConfig.api_protocol = "https://"
    nasdaqdatalink.ApiConfig.api_base = NASDAQ_API_BASE
    nasdaqdatalink.ApiConfig.verify_ssl = True


def manifest(context: Context) -> dict:
    """Return the manifest of ``GET /v1/manifest``."""
    return {
        "id": context.identity.id,
        "family": FAMILY,
        "version": context.identity.version,
        "contract_version": context.identity.contract_version,
        "contract_revision": context.identity.contract_revision,
        "capabilities": {"operations": list(operations.OPERATIONS), "max_concurrency": operations.MAX_CONCURRENCY},
    }


def health(context: Context) -> dict:
    """Return the health of ``GET /v1/health``. It is ``degraded`` when the host sent no API key."""
    return {"status": "ok" if context.api_key else "degraded", "tools": []}


def make_handler(context: Context, token: str, port: dict) -> type[BaseHTTPRequestHandler]:
    expected_authorization = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "meridian-provider-sharadar"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            logger.info("%s %s", getattr(self, "command", None), str(getattr(self, "path", "")).split("?", 1)[0])

        def _send(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            if status >= 400:
                self.send_header("Connection", "close")
            self.send_header("Content-Type", "application/problem+json" if status >= 400 else "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                self.wfile.write(body)

        def _problem(self, problem: Problem) -> None:
            self._send(problem.status, problem.body())

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            self._problem(Problem(code, "invalid_http_request"))

        def _allowed_method(self) -> str | None:
            if self.path in {"/v1/manifest", "/v1/health"}:
                return "GET"
            if self.path.startswith("/v1/") and self.path.removeprefix("/v1/") in operations.HANDLERS:
                return "POST"
            return None

        def _method_not_allowed(self, allow: str) -> None:
            problem = Problem(405, "method_not_allowed")
            self._send(problem.status, problem.body(), {"Allow": allow})

        def _unsupported_method(self) -> None:
            if not self._authorized():
                return
            allow = self._allowed_method()
            if allow is None:
                self._problem(Problem(404, "not_found"))
            else:
                self._method_not_allowed(allow)

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _unsupported_method

        def _authorized(self) -> bool:
            if self.headers.get("Origin") is not None:
                self._problem(Problem(403, "origin_rejected"))
                return False
            if self.headers.get("Host") not in {f"127.0.0.1:{port['value']}", f"localhost:{port['value']}"}:
                self._problem(Problem(421, "host_rejected"))
                return False
            supplied = (self.headers.get("Authorization") or "").encode("latin-1", "replace")
            if not secrets.compare_digest(supplied, expected_authorization):
                self._problem(Problem(401, "token_rejected"))
                return False
            return True

        def do_GET(self) -> None:
            if not self._authorized():
                return
            if self.path == "/v1/manifest":
                self._send(200, manifest(context))
            elif self.path == "/v1/health":
                self._send(200, health(context))
            elif self._allowed_method() == "POST":
                self._method_not_allowed("POST")
            else:
                self._problem(Problem(404, "not_found"))

        def do_POST(self) -> None:
            if not self._authorized():
                return
            if self._allowed_method() == "GET":
                self._method_not_allowed("GET")
                return
            handler = operations.HANDLERS.get(self.path.removeprefix("/v1/")) if self.path.startswith("/v1/") else None
            if handler is None:
                self._problem(Problem(404, "unsupported_operation"))
                return
            length_header = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding") is not None or length_header is None:
                self._problem(Problem(411, "length_required"))
                return
            try:
                length = int(length_header)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY_BYTES:
                self._problem(Problem(413, "request_too_large"))
                return
            try:
                body = loads_strict(self.rfile.read(length))
            except (UnicodeDecodeError, ValueError, RecursionError):
                self._problem(Problem(400, "invalid_json"))
                return
            try:
                self._send(200, handler(context, body))
            except Problem as problem:
                self._problem(problem)
            except Exception as exc:
                logger.error("Operation %s failed (exception_type=%s)", self.path, type(exc).__name__)
                self._problem(Problem(500, "internal_error"))

    return Handler


def serve(handshake: dict, identity: PluginIdentity, ready: BinaryIO, stdin: BinaryIO) -> int:
    """Take the lock, write the ready line and serve until stdin reaches end of file."""
    lock = os.open(handshake["lock_path"], os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        ready.write(b'{"error":"lock_held"}\n')
        ready.flush()
        os.close(lock)
        return 0
    configure_nasdaq_client()
    context = Context(
        identity=identity,
        api_key=handshake["secrets"].get(operations.CREDENTIAL_NAME) or None,
        cache_dir=handshake["grants"]["plugin_cache"],
    )
    port: dict = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(context, handshake["token"], port))
    server.daemon_threads = True
    port["value"] = server.server_address[1]

    def stop_on_eof() -> None:
        while stdin.read(65536):
            pass
        server.shutdown()

    threading.Thread(target=stop_on_eof, daemon=True).start()
    ready.write(json.dumps({"port": port["value"]}).encode("utf-8") + b"\n")
    ready.flush()
    ready.close()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        os.close(lock)
    return 0


def main(argv: list[str]) -> int:
    """Start the plug-in. ``argv[1]`` is the path of ``plugin.json``."""
    ready = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        if len(argv) != 2:
            raise StartError("usage: meridian-provider-sharadar <plugin.json>")
        identity = load_identity(Path(argv[1]))
        handshake = read_handshake(sys.stdin.buffer)
    except StartError as exc:
        logger.error("The plug-in did not start: %s", exc)
        return 2
    return serve(handshake, identity, ready, sys.stdin.buffer)
