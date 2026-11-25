import argparse
import contextlib
import fcntl
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from server.platform import config_store
from server.plugins import inventory, paths, registry

TRUST_STATEMENT = (
    "A plug-in runs with the rights of your user account. Meridian does not sandbox it. "
    "Confirm only a plug-in from a source that you trust."
)
PLUGIN_JSON_KEYS = frozenset(
    {
        "id",
        "version",
        "target",
        "family",
        "binding",
        "contract_version",
        "contract_revision",
        "command",
        "grants",
        "credentials",
        "external_tools",
    }
)
_ARCHITECTURES = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}
_SYSTEMS = {"Darwin": "apple-darwin", "Linux": "unknown-linux-gnu"}


class CliError(Exception):
    pass


def host_target() -> str:
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    system = _SYSTEMS.get(platform.system())
    if architecture is None or system is None:
        raise CliError(f"unsupported platform {platform.machine()} {platform.system()}")
    return f"{architecture}-{system}"


def _check_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if name.startswith("/") or "\\" in name:
        raise CliError(f"archive member {name!r} has an absolute or invalid path")
    parts = PurePosixPath(name).parts
    if any(part == ".." for part in parts):
        raise CliError(f"archive member {name!r} has a '..' segment")
    if member.isdev() or member.isfifo() or member.islnk():
        raise CliError(f"archive member {name!r} is a device, FIFO or hard link")
    if member.issym():
        relative = PurePosixPath(*[part for part in parts if part != "."]).as_posix()
        if not inventory.link_stays_inside(relative, member.linkname):
            raise CliError(f"archive link {name!r} leaves the version directory")
    elif not (member.isfile() or member.isdir()):
        raise CliError(f"archive member {name!r} has an unsupported type")


def _extract(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            for member in members:
                _check_member(member)
            bundle.extractall(destination, members=members, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise CliError(f"the archive cannot be extracted: {type(exc).__name__}") from exc


def _read_plugin_json(root: Path) -> dict:
    try:
        document = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise CliError("plugin.json is missing or is not UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != PLUGIN_JSON_KEYS:
        raise CliError("plugin.json keys differ from the plugin-runtime contract")
    if not paths.valid_id(document["id"]) or not paths.valid_semver(document["version"]):
        raise CliError("plugin.json has an invalid id or version")
    command = document["command"]
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise CliError("plugin.json command must be a non-empty list of strings")
    if not inventory.valid_relative(command[0]):
        raise CliError("plugin.json command[0] must be a path inside the version directory")
    tools = document["external_tools"]
    if not isinstance(tools, list) or not all(isinstance(tool, str) and tool for tool in tools):
        raise CliError("plugin.json external_tools must be a list of strings")
    return document


def _entry_from_plugin(document: dict, version_root: Path, inventory_sha256: str) -> dict:
    entry = {
        "id": document["id"],
        "family": document["family"],
        "binding": document["binding"],
        "contract_version": document["contract_version"],
        "contract_revision": document["contract_revision"],
        "version": document["version"],
        "command": [str(version_root / document["command"][0]), *document["command"][1:]],
        "url": None,
        "token_file": None,
        "inventory_sha256": inventory_sha256,
        "grants": document["grants"],
        "credentials": document["credentials"],
        "confirmed": None,
    }
    try:
        registry.parse_entry(entry)
    except registry.RegistryError as exc:
        raise CliError(f"plugin.json does not give a valid registry entry: {exc}") from exc
    return entry


def _verify_tree(root: Path) -> str:
    sums = root / inventory.SUMS_NAME
    if not sums.is_file():
        raise CliError(f"{inventory.SUMS_NAME} is missing")
    digest = inventory.file_sha256(sums)
    changed = inventory.differences(root, digest)
    if changed:
        raise CliError(f"the files differ from {inventory.SUMS_NAME}: {', '.join(changed[:20])}")
    return digest


@contextlib.contextmanager
def _registry_lock():
    home = paths.plugin_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    descriptor = os.open(home / "registry.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _load_document() -> dict:
    path = paths.registry_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {"schema_version": registry.SCHEMA_VERSION, "plugins": []}
    try:
        return registry.parse_registry(data)
    except registry.RegistryError as exc:
        raise CliError(f"the registry is invalid and is not changed: {exc}") from exc


def _document_with(entry: dict) -> dict:
    document = _load_document()
    target = registry.binding_key(entry["family"], entry["binding"])
    for other in document["plugins"]:
        if (
            isinstance(other, dict)
            and other.get("id") != entry["id"]
            and isinstance(other.get("binding"), dict)
            and registry.binding_key(str(other.get("family")), other["binding"]) == target
        ):
            raise CliError(f"the plug-in {other.get('id')!r} already has this binding")
    plugins = [
        other for other in document["plugins"] if not (isinstance(other, dict) and other.get("id") == entry["id"])
    ]
    plugins.append(entry)
    document["plugins"] = plugins
    return document


def _write_document(document: dict) -> None:
    config_store._atomic_write_json(paths.registry_path(), document, mode=0o600)


def _describe(entry: dict, external_tools: list[str] | None, out) -> None:
    print("Plug-in registry entry:", file=out)
    print(f"  id:            {entry['id']}", file=out)
    print(f"  version:       {entry['version']}", file=out)
    print(f"  family:        {entry['family']}", file=out)
    print(f"  binding:       {json.dumps(entry['binding'], sort_keys=True)}", file=out)
    print(f"  contract:      {entry['contract_version']} revision {entry['contract_revision']}", file=out)
    if entry["url"] is None:
        print(f"  command:       {json.dumps(entry['command'])}", file=out)
    else:
        print(f"  url:           {entry['url']}", file=out)
        print(f"  token file:    {entry['token_file']}", file=out)
    print(f"  grants:        {', '.join(entry['grants']) or 'none'}", file=out)
    secrets = [
        f"{name}: {spec['secret']}" + (f" (environment {spec['env']})" if spec["env"] else "")
        for name, spec in sorted(entry["credentials"].items())
    ]
    print(f"  secrets:       {'; '.join(secrets) or 'none'}", file=out)
    print(f"  inventory:     {entry['inventory_sha256']}", file=out)
    if external_tools is not None:
        print(f"  tools:         {json.dumps(external_tools)}", file=out)
    print(TRUST_STATEMENT, file=out)


def _confirm(entry: dict, external_tools: list[str] | None, stdin, out) -> dict:
    _describe(entry, external_tools, out)
    print("Type yes to confirm this entry: ", end="", file=out, flush=True)
    answer = stdin.readline().strip()
    if answer != "yes":
        raise CliError("not confirmed; the registry is not changed")
    confirmed = dict(entry)
    confirmed["confirmed"] = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry_sha256": registry.entry_sha256(entry),
    }
    return confirmed


def _require_terminal(stdin) -> None:
    if not stdin.isatty():
        raise CliError("confirmation needs a terminal; stdin is not a terminal")


def install(archive: Path, sha256: str, stdin, out) -> None:
    _require_terminal(stdin)
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256.lower()):
        raise CliError("--sha256 must be 64 hexadecimal characters")
    if not archive.is_file():
        raise CliError(f"{archive} is not a file")
    if inventory.file_sha256(archive) != sha256.lower():
        raise CliError("the archive digest differs from --sha256")
    home = paths.plugin_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=home))
    try:
        _extract(archive, staging)
        document = _read_plugin_json(staging)
        if document["target"] != host_target():
            raise CliError(f"the archive target {document['target']} differs from this platform {host_target()}")
        digest = _verify_tree(staging)
        version_root = paths.version_dir(document["id"], document["version"])
        if version_root.exists():
            raise CliError(f"{document['id']} {document['version']} is already installed; use 'plugin use'")
        entry = _entry_from_plugin(document, version_root, digest)
        confirmed = _confirm(entry, document["external_tools"], stdin, out)
        with _registry_lock():
            updated = _document_with(confirmed)
            version_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            version_root.parent.chmod(0o700)
            if version_root.exists():
                raise CliError(f"{document['id']} {document['version']} is already installed")
            os.rename(staging, version_root)
            cache = paths.cache_dir(document["id"])
            cache.mkdir(exist_ok=True, mode=0o700)
            cache.chmod(0o700)
            _write_document(updated)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    print(f"Installed and confirmed {document['id']} {document['version']}.", file=out)


def use(plugin_id: str, version: str, stdin, out) -> None:
    _require_terminal(stdin)
    version_root = paths.version_dir(plugin_id, version)
    if not version_root.is_dir():
        raise CliError(f"{plugin_id} {version} is not installed")
    document = _read_plugin_json(version_root)
    if document["id"] != plugin_id or document["version"] != version or document["target"] != host_target():
        raise CliError("plugin.json does not match this version directory or platform")
    entry = _entry_from_plugin(document, version_root, _verify_tree(version_root))
    confirmed = _confirm(entry, document["external_tools"], stdin, out)
    with _registry_lock():
        _write_document(_document_with(confirmed))
    print(f"Plug-in {plugin_id} now uses version {version}.", file=out)


def confirm(plugin_id: str, stdin, out) -> None:
    _require_terminal(stdin)
    document = _load_document()
    raw = next((item for item in document["plugins"] if isinstance(item, dict) and item.get("id") == plugin_id), None)
    if raw is None:
        raise CliError(f"no registry entry has the id {plugin_id!r}")
    try:
        entry = registry.parse_entry(raw)
    except registry.RegistryError as exc:
        raise CliError(f"the registry entry is invalid: {exc}") from exc
    if entry.command is not None:
        digest = _verify_tree(paths.version_dir(entry.id, entry.version))
        if digest != entry.inventory_sha256:
            raise CliError("the installed files differ from the inventory digest of the entry")
    unconfirmed = {key: value for key, value in raw.items() if key != "confirmed"}
    unconfirmed["confirmed"] = None
    confirmed = _confirm(unconfirmed, None, stdin, out)
    with _registry_lock():
        current = _load_document()
        if not any(isinstance(item, dict) and item == raw for item in current["plugins"]):
            raise CliError("the registry entry changed during the confirmation; run the command again")
        _write_document(_document_with(confirmed))
    print(f"Confirmed {plugin_id}.", file=out)


def main(argv: list[str], stdin=None, out=None) -> int:
    stdin = stdin or sys.stdin
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="meridian-backend plugin", description="Install and confirm Meridian plug-ins"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install", help="Install a plug-in archive and confirm its registry entry")
    install_parser.add_argument("archive", type=Path)
    install_parser.add_argument("--sha256", required=True, help="SHA-256 of the archive from the release")
    confirm_parser = commands.add_parser("confirm", help="Confirm a registry entry again")
    confirm_parser.add_argument("id")
    use_parser = commands.add_parser("use", help="Use an installed version of a plug-in (quit Meridian first)")
    use_parser.add_argument("id")
    use_parser.add_argument("version")
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            install(args.archive, args.sha256, stdin, out)
        elif args.command == "confirm":
            confirm(args.id, stdin, out)
        else:
            use(args.id, args.version, stdin, out)
    except (CliError, paths.PluginPathError, inventory.InventoryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
