import errno
import fcntl
import json
import logging
import os
import re as _re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


_SAFE_KEY = _re.compile(r"^[a-zA-Z0-9_-]+$")

_CONFIG_LOCK_TIMEOUT_SECONDS = 5.0
_CONFIG_LOCK_POLL_SECONDS = 0.05
_LEGACY_CONFIG_META = {"deviceId": "legacy", "writtenAt": None, "version": 0}
_LOGICAL_TICKS_PER_MILLISECOND = 1_000_000


class ConfigStoreError(RuntimeError):
    """A persisted config could not be read or updated without risking data."""


class ConfigLockTimeout(ConfigStoreError):
    """The kernel-owned config transaction lock remained busy until timeout."""


def _json_clone(value):
    return json.loads(json.dumps(value))


def _replace_file_durably(source: Path, destination: Path) -> None:
    """Replace a file without acknowledging before the directory entry is durable."""
    if os.name == "nt":
        import ctypes

        move_file_ex = ctypes.windll.kernel32.MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file_ex.restype = ctypes.c_int
        movefile_replace_existing = 0x1
        movefile_write_through = 0x8
        if not move_file_ex(
            str(source),
            str(destination),
            movefile_replace_existing | movefile_write_through,
        ):
            raise ctypes.WinError()
        return

    os.replace(source, destination)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(destination.parent, directory_flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_existing_file_durably(path: Path) -> None:
    """Re-establish durability before acknowledging an unchanged existing value."""
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name == "nt":
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, directory_flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict, *, mode: int = 0o600) -> None:
    """Durably replace one JSON file without exposing a partial generation.

    The temporary file is flushed to stable storage before replacement. The
    parent directory is then synced so the rename itself survives a host crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)
            file.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(file.fileno(), mode)
            os.fsync(file.fileno())
        tmp.chmod(mode)
        _replace_file_durably(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _config_now_iso() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _quarantine_corrupt_config(path: Path) -> Path:
    """Preserve corrupt bytes privately before an explicit user repair."""
    raw = path.read_bytes()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = path.with_name(f"{path.stem}.corrupt-{stamp}-{uuid.uuid4().hex}.json")
    descriptor = os.open(quarantine, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        quarantine.chmod(0o600)
    except Exception:
        quarantine.unlink(missing_ok=True)
        raise
    return quarantine


def _config_error_detail(key: str, code: str = "config_corrupt") -> dict[str, str]:
    """Return a publication-safe config error without exposing filesystem state."""
    return {"code": code, "key": key}


def _safe_config_version(value) -> int:
    try:
        version = int(value)
    except Exception:
        return 0
    return max(0, version)


def _config_blob_meta(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("_meta"), dict):
        return dict(_LEGACY_CONFIG_META)
    meta = value.get("_meta") or {}
    return {
        "deviceId": str(meta.get("deviceId") or "legacy"),
        "writtenAt": meta.get("writtenAt"),
        "version": _safe_config_version(meta.get("version", 0)),
    }


def _strip_config_meta(value):
    if not isinstance(value, dict):
        return _json_clone(value)
    if "_meta" in value and set(value.keys()) <= {"_meta", "value"}:
        return _json_clone(value.get("value"))
    result = _json_clone(value)
    result.pop("_meta", None)
    return result


def _with_config_meta(value, meta: dict) -> dict:
    if isinstance(value, dict):
        result = _json_clone(value)
        result["_meta"] = _json_clone(meta)
        return result
    return {"value": _json_clone(value), "_meta": _json_clone(meta)}


def _should_take_incoming(existing_meta: dict, incoming_meta: dict) -> bool:
    existing = {**_LEGACY_CONFIG_META, **(existing_meta or {})}
    incoming = {**_LEGACY_CONFIG_META, **(incoming_meta or {})}
    existing_version = _safe_config_version(existing.get("version"))
    incoming_version = _safe_config_version(incoming.get("version"))
    if incoming_version > existing_version:
        return True
    if incoming_version < existing_version:
        return False

    existing_device = str(existing.get("deviceId") or "legacy")
    incoming_device = str(incoming.get("deviceId") or "legacy")
    if incoming_device > existing_device:
        return True
    if incoming_device < existing_device:
        return False

    return _timestamp_value(incoming.get("writtenAt")) >= _timestamp_value(existing.get("writtenAt"))


def _take_incoming_timestamp(current_ts: float, incoming_ts: float, incoming_wins_tie: bool) -> bool:
    """Resolve equal field timestamps with the enclosing delivery order."""
    if incoming_ts > current_ts:
        return True
    if incoming_ts < current_ts:
        return False
    return incoming_wins_tie


def _incoming_config_meta(key: str, value, device_id: str | None, versions: dict | None) -> dict:
    value_meta = _config_blob_meta(value)
    version = value_meta.get("version", 0)
    if isinstance(versions, dict) and key in versions:
        version = versions.get(key)
    return {
        "deviceId": str(device_id or value_meta.get("deviceId") or "legacy"),
        "writtenAt": _config_now_iso(),
        "version": _safe_config_version(version),
    }


def _open_config_lock(lock_path: Path) -> int:
    """Open the stable inode used for the OS-owned advisory lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path.parent.chmod(0o700)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise ConfigStoreError("config lock could not be opened") from exc
    try:
        os.chmod(lock_path, 0o600)
    except OSError:
        os.close(fd)
        raise
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
        os.fsync(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def _try_config_lock(fd: int) -> bool:
    """Try to acquire the lock on one open file description."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise ConfigStoreError("config lock could not be acquired") from exc


def _release_config_lock(fd: int) -> None:
    """Release the POSIX OS-owned advisory lock."""
    fcntl.flock(fd, fcntl.LOCK_UN)


def _wait_and_acquire_config_lock(lock_path: Path, owner_id: str) -> int:
    """Acquire an OS-owned exclusive lock or fail without authorizing a write.

    The lock file is a stable rendezvous inode and is deliberately never
    deleted. Kernel ownership, not file age or file contents, determines
    exclusivity; a crashed process therefore releases the lock automatically,
    while a slow live writer can never be evicted by elapsed wall time.
    """
    fd = _open_config_lock(lock_path)
    start = time.monotonic()
    try:
        while not _try_config_lock(fd):
            if time.monotonic() - start >= _CONFIG_LOCK_TIMEOUT_SECONDS:
                raise ConfigLockTimeout("timed out waiting for config lock")
            time.sleep(_CONFIG_LOCK_POLL_SECONDS)
        owner = owner_id.encode("utf-8")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, owner)
            os.ftruncate(fd, len(owner))
            os.fsync(fd)
        except OSError as exc:
            try:
                _release_config_lock(fd)
            finally:
                raise ConfigStoreError("config lock owner could not be recorded") from exc
        return fd
    except Exception:
        os.close(fd)
        raise


@contextmanager
def _config_write_lock(path: Path, device_id: str):
    lock_path = path.with_suffix(".lock")
    owner_id = f"{device_id}:{uuid.uuid4().hex}"
    fd = _wait_and_acquire_config_lock(lock_path, owner_id)
    try:
        yield
    finally:
        try:
            _release_config_lock(fd)
        except OSError:
            logger.exception("Config lock cleanup failed")
        finally:
            os.close(fd)


def _write_locked_config_json(path: Path, payload: dict, device_id: str) -> None:
    with _config_write_lock(path, device_id):
        _atomic_write_json(path, payload)


def _timestamp_value(value):
    if not value:
        return 0
    if isinstance(value, str):
        logical_match = _re.fullmatch(r"nwts:(0|[1-9][0-9]*)", value)
        if logical_match is not None:
            return int(logical_match.group(1))
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = parsed - epoch
        milliseconds = delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000
        return milliseconds * _LOGICAL_TICKS_PER_MILLISECOND
    except Exception:
        return 0


def _latest_timestamp(a, b):
    return a if _timestamp_value(a) >= _timestamp_value(b) else b


def _merge_timestamp_map(current=None, incoming=None):
    current = current or {}
    incoming = incoming or {}
    return {key: _latest_timestamp(current.get(key), incoming.get(key)) for key in set(current) | set(incoming)}


def _config_payloads_equal(left: object, right: object) -> bool:
    """Compare persisted config meaning while ignoring delivery metadata."""
    return _strip_config_meta(left) == _strip_config_meta(right)


def _read_user_config_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigStoreError("saved config could not be read as JSON") from exc


def _user_config_exists(path: Path) -> bool:
    """Distinguish true absence from an existing but unreadable path."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ConfigStoreError("saved config path could not be inspected") from exc
    return True
