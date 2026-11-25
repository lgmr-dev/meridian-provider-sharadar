import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SUMS_NAME = "SHA256SUMS"
_FILE_LINE = re.compile(r"([0-9a-f]{64})  (.+)")
_LINK_LINE = re.compile(r"link:(.+)  (.+)")


class InventoryError(ValueError):
    pass


@dataclass(frozen=True)
class InventoryItem:
    path: str
    sha256: str | None
    link_target: str | None


def valid_relative(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def link_stays_inside(link_path: str, target: str) -> bool:
    if not target or target.startswith("/") or "\\" in target:
        return False
    depth = len(PurePosixPath(link_path).parent.parts)
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        depth = depth - 1 if part == ".." else depth + 1
        if depth < 0:
            return False
    return True


def parse_sums(data: bytes) -> dict[str, InventoryItem]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InventoryError(f"{SUMS_NAME} is not UTF-8") from exc
    if text and not text.endswith("\n"):
        raise InventoryError(f"{SUMS_NAME} must end with a newline")
    items: dict[str, InventoryItem] = {}
    for line in text.splitlines():
        if match := _FILE_LINE.fullmatch(line):
            item = InventoryItem(path=match.group(2), sha256=match.group(1), link_target=None)
        elif match := _LINK_LINE.fullmatch(line):
            item = InventoryItem(path=match.group(2), sha256=None, link_target=match.group(1))
            if not link_stays_inside(item.path, item.link_target):
                raise InventoryError(f"link {item.path} leaves the version directory")
        else:
            raise InventoryError(f"{SUMS_NAME} has an invalid line")
        if not valid_relative(item.path) or item.path == SUMS_NAME:
            raise InventoryError(f"{SUMS_NAME} lists an invalid path")
        if item.path in items:
            raise InventoryError(f"{SUMS_NAME} lists {item.path} twice")
        items[item.path] = item
    return items


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan(root: Path) -> dict[str, InventoryItem]:
    found: dict[str, InventoryItem] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            try:
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    found[relative] = InventoryItem(path=relative, sha256=None, link_target=os.readlink(path))
                elif stat.S_ISREG(mode):
                    found[relative] = InventoryItem(path=relative, sha256=file_sha256(path), link_target=None)
                elif not stat.S_ISDIR(mode):
                    found[relative] = InventoryItem(path=relative, sha256="special", link_target=None)
            except OSError:
                found[relative] = InventoryItem(path=relative, sha256="unreadable", link_target=None)
    found.pop(SUMS_NAME, None)
    return found


def build_sums(root: Path) -> bytes:
    lines = []
    for relative, item in sorted(scan(root).items()):
        if item.link_target is not None:
            if not link_stays_inside(relative, item.link_target):
                raise InventoryError(f"link {relative} leaves the version directory")
            lines.append(f"link:{item.link_target}  {relative}\n")
        elif item.sha256 in {"special", "unreadable"}:
            raise InventoryError(f"{relative} is not a readable regular file, directory or link")
        else:
            lines.append(f"{item.sha256}  {relative}\n")
    return "".join(lines).encode("utf-8")


def differences(root: Path, inventory_sha256: str) -> list[str]:
    sums_path = root / SUMS_NAME
    try:
        data = sums_path.read_bytes()
    except OSError:
        return [SUMS_NAME]
    if hashlib.sha256(data).hexdigest() != inventory_sha256:
        return [SUMS_NAME]
    try:
        expected = parse_sums(data)
    except InventoryError:
        return [SUMS_NAME]
    actual = scan(root)
    changed = sorted(set(expected) ^ set(actual))
    for relative in sorted(set(expected) & set(actual)):
        if expected[relative] != actual[relative]:
            changed.append(relative)
    return changed
