import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "aa5d0cbc4d0baef662e91cc087d8a15e97a76e3e"


def git_blob_id(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def pinned_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "SOURCE.json" and "__pycache__" not in path.parts
    }


@pytest.mark.parametrize("bundle", ["tests/reference", "contracts"])
def test_bundle_holds_the_meridian_files_of_the_source_commit(bundle: str) -> None:
    root = ROOT / bundle
    source = json.loads((root / "SOURCE.json").read_text(encoding="utf-8"))

    assert source["meridian_commit"] == SOURCE_COMMIT
    assert pinned_files(root) == set(source["files"])
    for relative, blob_id in source["files"].items():
        assert git_blob_id(root / relative) == blob_id, relative
