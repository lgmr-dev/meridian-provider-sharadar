import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from server.platform.http import _is_loopback_origin
from server.plugins import paths

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FAMILIES = ("tax", "market-data")
GRANTS = {"tax": frozenset({"tax_dir", "plugin_cache"}), "market-data": frozenset({"plugin_cache"})}
ENTRY_KEYS = frozenset(
    {
        "id",
        "family",
        "binding",
        "contract_version",
        "contract_revision",
        "version",
        "command",
        "url",
        "token_file",
        "inventory_sha256",
        "grants",
        "credentials",
        "confirmed",
    }
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[A-Za-z0-9._-]{1,128}")
_TERRITORY = re.compile(r"[A-Z]{2}")
_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SECRET_NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_AUXILIARY_BINDINGS = frozenset({"fii_fundamentals"})


class RegistryError(ValueError):
    pass


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def entry_sha256(raw_entry: dict) -> str:
    return canonical_sha256({key: value for key, value in raw_entry.items() if key != "confirmed"})


def binding_key(family: str, binding: dict) -> str:
    return f"{family}:{canonical_sha256(binding)}"


@dataclass(frozen=True)
class Entry:
    id: str
    family: str
    binding: dict
    contract_version: str
    contract_revision: str
    version: str
    command: tuple[str, ...] | None
    url: str | None
    token_file: str | None
    inventory_sha256: str | None
    grants: tuple[str, ...]
    credentials: dict
    confirmed: bool
    entry_sha256: str


@dataclass(frozen=True)
class Snapshot:
    file_sha256: str | None
    entries: dict[str, Entry] = field(default_factory=dict)
    valid: bool = True


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RegistryError(message)


def _valid_path_text(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _has_port(location) -> bool:
    try:
        return location.port is not None
    except ValueError:
        return False


def _valid_binding(family: str, binding: object) -> bool:
    if not isinstance(binding, dict) or len(binding) != 1:
        return False
    ((key, value),) = binding.items()
    if not isinstance(value, str):
        return False
    if family == "tax":
        return key == "territory" and _TERRITORY.fullmatch(value) is not None
    if key == "provider_id":
        return _PROVIDER_ID.fullmatch(value) is not None
    return key == "auxiliary" and value in _AUXILIARY_BINDINGS


def _valid_credentials(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for name, spec in value.items():
        if not isinstance(name, str) or _SECRET_NAME.fullmatch(name) is None:
            return False
        if not isinstance(spec, dict) or set(spec) != {"secret", "env"}:
            return False
        if not isinstance(spec["secret"], str) or _SECRET_NAME.fullmatch(spec["secret"]) is None:
            return False
        if spec["env"] is not None and (not isinstance(spec["env"], str) or _ENV_NAME.fullmatch(spec["env"]) is None):
            return False
    return True


def parse_entry(raw: object) -> Entry:
    _require(isinstance(raw, dict), "entry must be an object")
    _require(set(raw) == ENTRY_KEYS, "entry keys differ from schema version 1")
    _require(paths.valid_id(raw["id"]), "invalid id")
    family = raw["family"]
    _require(family in FAMILIES, "unknown family")
    _require(_valid_binding(family, raw["binding"]), "invalid binding")
    _require(paths.valid_semver(raw["contract_version"]), "invalid contract_version")
    _require(
        isinstance(raw["contract_revision"], str) and _REVISION.fullmatch(raw["contract_revision"]) is not None,
        "invalid contract_revision",
    )
    _require(paths.valid_semver(raw["version"]), "invalid version")
    command, url = raw["command"], raw["url"]
    _require((command is None) != (url is None), "exactly one of command and url is required")
    if command is not None:
        _require(
            isinstance(command, list) and bool(command) and all(_valid_path_text(part) for part in command),
            "command must be a non-empty list of strings without NUL or lone surrogates",
        )
        _require(Path(command[0]).is_absolute(), "command[0] must be an absolute path")
        _require(
            isinstance(raw["inventory_sha256"], str) and _HEX64.fullmatch(raw["inventory_sha256"]) is not None,
            "a command entry needs inventory_sha256",
        )
        _require(raw["token_file"] is None, "a command entry has no token_file")
    else:
        try:
            location = urlsplit(url) if isinstance(url, str) else None
        except ValueError:
            location = None
        _require(
            location is not None and location.scheme == "http" and _has_port(location) and _is_loopback_origin(url),
            "url must be an http loopback origin with a port",
        )
        _require(
            _valid_path_text(raw["token_file"]) and Path(raw["token_file"]).is_absolute(),
            "a url entry needs an absolute token_file",
        )
        _require(raw["inventory_sha256"] is None, "a url entry has no inventory_sha256")
    grants = raw["grants"]
    _require(
        isinstance(grants, list)
        and all(isinstance(grant, str) for grant in grants)
        and len(set(grants)) == len(grants)
        and set(grants) <= GRANTS[family],
        "invalid grants",
    )
    _require(_valid_credentials(raw["credentials"]), "invalid credentials")
    confirmed = raw["confirmed"]
    digest = entry_sha256(raw)
    if confirmed is not None:
        _require(
            isinstance(confirmed, dict)
            and set(confirmed) == {"at", "entry_sha256"}
            and isinstance(confirmed["at"], str)
            and isinstance(confirmed["entry_sha256"], str),
            "invalid confirmed",
        )
    return Entry(
        id=raw["id"],
        family=family,
        binding=dict(raw["binding"]),
        contract_version=raw["contract_version"],
        contract_revision=raw["contract_revision"],
        version=raw["version"],
        command=tuple(command) if command is not None else None,
        url=url,
        token_file=raw["token_file"],
        inventory_sha256=raw["inventory_sha256"],
        grants=tuple(grants),
        credentials={name: dict(spec) for name, spec in raw["credentials"].items()},
        confirmed=confirmed is not None and confirmed["entry_sha256"] == digest,
        entry_sha256=digest,
    )


def parse_registry(data: bytes) -> dict:
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise RegistryError("registry is not UTF-8 JSON") from exc
    _require(isinstance(document, dict), "registry must be an object")
    _require(set(document) == {"schema_version", "plugins"}, "registry keys differ from schema version 1")
    _require(
        type(document["schema_version"]) is int and document["schema_version"] == SCHEMA_VERSION,
        "unknown schema_version",
    )
    _require(isinstance(document["plugins"], list), "plugins must be a list")
    return document


def entries_from_document(document: dict) -> dict[str, Entry]:
    parsed: list[Entry] = []
    for index, raw in enumerate(document["plugins"]):
        try:
            parsed.append(parse_entry(raw))
        except RegistryError as exc:
            logger.warning("Plug-in registry entry %d is invalid: %s", index, exc)
    id_counts: dict[str, int] = {}
    binding_counts: dict[str, int] = {}
    for entry in parsed:
        id_counts[entry.id] = id_counts.get(entry.id, 0) + 1
        key = binding_key(entry.family, entry.binding)
        binding_counts[key] = binding_counts.get(key, 0) + 1
    entries: dict[str, Entry] = {}
    for entry in parsed:
        if id_counts[entry.id] > 1 or binding_counts[binding_key(entry.family, entry.binding)] > 1:
            logger.warning("Plug-in registry entry %s has a duplicate id or binding and is disabled", entry.id)
            continue
        entries[entry.id] = entry
    return entries


def read_registry() -> Snapshot:
    try:
        path = paths.registry_path()
    except paths.PluginPathError as exc:
        logger.warning("Plug-in registry path is invalid: %s", exc)
        return Snapshot(file_sha256=None, valid=False)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Snapshot(file_sha256=None)
    except OSError as exc:
        logger.warning("Plug-in registry cannot be read: %s", type(exc).__name__)
        return Snapshot(file_sha256=None, valid=False)
    file_sha256 = hashlib.sha256(data).hexdigest()
    try:
        document = parse_registry(data)
    except RegistryError as exc:
        logger.warning("Plug-in registry is disabled: %s", exc)
        return Snapshot(file_sha256=file_sha256, valid=False)
    return Snapshot(file_sha256=file_sha256, entries=entries_from_document(document))


def registry_file_sha256() -> str | None:
    try:
        return hashlib.sha256(paths.registry_path().read_bytes()).hexdigest()
    except (FileNotFoundError, paths.PluginPathError):
        return None
    except OSError:
        return "unreadable"
