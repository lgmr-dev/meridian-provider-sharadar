import os
import re
from pathlib import Path

REGISTRY_ENV = "MERIDIAN_PLUGIN_REGISTRY"
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SEMVER_PATTERN = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
    r"(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
)


class PluginPathError(ValueError):
    pass


def valid_id(value: object) -> bool:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def valid_semver(value: object) -> bool:
    return isinstance(value, str) and SEMVER_PATTERN.fullmatch(value) is not None


def registry_path() -> Path:
    configured = os.environ.get(REGISTRY_ENV, "").strip()
    if not configured:
        return Path.home() / ".meridian" / "plugins" / "registry.json"
    path = Path(configured)
    if not path.is_absolute():
        raise PluginPathError(f"{REGISTRY_ENV} must be an absolute path")
    return path


def plugin_home() -> Path:
    return registry_path().parent


def plugin_dir(plugin_id: str) -> Path:
    if not valid_id(plugin_id):
        raise PluginPathError("invalid plug-in id")
    return plugin_home() / plugin_id


def version_dir(plugin_id: str, version: str) -> Path:
    if not valid_semver(version):
        raise PluginPathError("invalid plug-in version")
    return plugin_dir(plugin_id) / version


def cache_dir(plugin_id: str) -> Path:
    return plugin_dir(plugin_id) / "cache"


def lock_path(plugin_id: str) -> Path:
    return plugin_dir(plugin_id) / "plugin.lock"


def tax_dir(data_dir: str | Path, territory: str) -> Path:
    base = Path(data_dir) / "tax"
    return base if territory == "DE" else base / territory


def is_inside_plugin_home(directory: str | Path) -> bool:
    try:
        home = plugin_home().expanduser().resolve()
    except PluginPathError:
        return False
    candidate = Path(directory).expanduser().resolve()
    return candidate == home or candidate.is_relative_to(home)


def refuse_data_dir_inside_plugin_home(data_dir: str | Path) -> None:
    if is_inside_plugin_home(data_dir):
        raise RuntimeError(
            f"The data directory {Path(data_dir).expanduser().resolve()} is inside the plug-in home "
            f"{plugin_home().expanduser().resolve()}; choose a data directory outside it"
        )
