import hashlib
import json
import os
from pathlib import Path

# Runtime state contains financial records, tax documents, credentials, and
# browser data. Files created by this process must be private even when the
# caller's shell has a permissive default umask.
os.umask(0o077)

MERIDIAN_RC = Path.home() / ".meridian.json"


def _read_json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} could not be read as JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return value


def _resolve_data_dir() -> str:
    """Resolve data directory: DATA_DIR env var > ~/.meridian.json > ./data."""
    env = os.environ.get("DATA_DIR", "").strip()
    if env:
        return env
    if MERIDIAN_RC.exists():
        rc = _read_json_object(MERIDIAN_RC, "Meridian config")
        d = rc.get("data_dir", "")
        if not isinstance(d, str):
            raise RuntimeError("Meridian config data_dir must be a string")
        if d.strip():
            return d.strip()
    return "./data"


class _AppState:
    data_dir: str = ""
    pending_data_dir: str | None = None


_state = _AppState()
_state.data_dir = _resolve_data_dir()


def get_data_dir() -> str:
    return _state.data_dir


def _data_namespace_id() -> str:
    """Return an opaque, stable identity for the active config namespace."""
    resolved = str(Path(get_data_dir()).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return f"dir:v1:{digest}"
