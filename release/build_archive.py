"""Build the release archive ``<id>-<version>-<target>.tar.gz`` of `contracts/README.md`.

Run from the repository root: ``python -m release.build_archive --version 1.0.0-rc.1 --out dist``.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "meridian-provider-sharadar"
SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
)
TARGETS = {("Darwin", "arm64"): "aarch64-apple-darwin", ("Linux", "x86_64"): "x86_64-unknown-linux-gnu"}
CONTRACT_FILES = ["LICENSE", "plugin-runtime/v1/openapi.json", "market-data/v1/openapi.json"]
SUMS_NAME = "SHA256SUMS"
NOTICE_WORDS = ("LICENSE", "COPYING", "NOTICE", "AUTHORS")
BUILD_DISTRIBUTIONS = ["pyinstaller"]
SHARED_LIBRARY = re.compile(r"\.so(\.[0-9.]+)?$|\.dylib$")


def host_target() -> str:
    target = TARGETS.get((platform.system(), platform.machine()))
    if target is None:
        raise SystemExit(f"unsupported build platform {platform.system()} {platform.machine()}")
    return target


def plugin_json(version: str, target: str) -> dict:
    contract = json.loads((ROOT / "contracts" / "market-data" / "v1" / "contract.json").read_text(encoding="utf-8"))
    return {
        "id": PLUGIN_ID,
        "version": version,
        "target": target,
        "family": "market-data",
        "binding": {"provider_id": "sharadar"},
        "contract_version": contract["contract_version"],
        "contract_revision": contract["revision"],
        "command": [f"bin/{PLUGIN_ID}/{PLUGIN_ID}", "plugin.json"],
        "grants": ["plugin_cache"],
        "credentials": {"api_key": {"secret": "sharadar_api_key", "env": "SHARADAR_API_KEY"}},
        "external_tools": [],
    }


def freeze(work: Path) -> Path:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(ROOT / "meridian_provider_sharadar" / "__main__.py"),
            "--onedir",
            "--name",
            PLUGIN_ID,
            "--paths",
            str(ROOT),
            "--distpath",
            str(work / "dist"),
            "--workpath",
            str(work / "build"),
            "--specpath",
            str(work),
            "--noconfirm",
            "--clean",
            "--exclude-module",
            "pytest",
            "--exclude-module",
            "setuptools",
            "--exclude-module",
            "readline",
            "--log-level",
            "WARN",
        ],
        check=True,
        stdout=sys.stderr,
    )
    return work / "dist" / PLUGIN_ID


def runtime_distributions() -> list[tuple[str, str]]:
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    return re.findall(r"^([A-Za-z0-9._-]+)==[^ ;\\]+(?: ; ([^\\]+?))? \\$", lock, flags=re.MULTILINE)


def stage_distribution_licenses(root: Path) -> set[str]:
    owned = set()
    for name, marker in runtime_distributions() + [(name, "") for name in BUILD_DISTRIBUTIONS]:
        if marker and not Marker(marker).evaluate():
            print(f"{name} is not for this platform ({marker}), so the archive holds no copy of it", file=sys.stderr)
            continue
        distribution = importlib.metadata.distribution(name)
        files = distribution.files or []
        owned.update(Path(item.name).name for item in files)
        notices = [item for item in files if any(word in item.name.upper() for word in NOTICE_WORDS)]
        if not notices:
            raise SystemExit(f"{name} has no license file")
        for item in notices:
            destination = root / "licenses" / name / item.as_posix()
            if destination.exists():
                raise SystemExit(f"two license files of {name} have the path {item.as_posix()}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(distribution.locate_file(item), destination)
    return owned


def stage_python_notices(root: Path, target: str) -> None:
    version = ".".join(str(part) for part in sys.version_info[:3])
    cpython = ROOT / "notices" / f"cpython-{version}" / "license.rst"
    if not cpython.is_file():
        raise SystemExit(f"notices/ has no cpython-{version}/license.rst")
    (root / "licenses" / "python").mkdir(parents=True)
    shutil.copyfile(cpython, root / "licenses" / "python" / "cpython-license.rst")
    marker = Path(sys.base_prefix) / "BUILD"
    if marker.is_file():
        folder = ROOT / "notices" / "python-build-standalone" / f"{marker.read_text(encoding='utf-8').strip()}-{target}"
        if not folder.is_dir():
            raise SystemExit(f"notices/python-build-standalone has no folder {folder.name}")
        shutil.copytree(folder, root / "licenses" / "python", dirs_exist_ok=True)
        return
    license_file = Path(sys.base_prefix) / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "LICENSE.txt"
    if not license_file.is_file():
        raise SystemExit("the Python of this build has no LICENSE.txt")
    shutil.copyfile(license_file, root / "licenses" / "python" / "LICENSE.txt")


def debian_package(library: str) -> str:
    result = subprocess.run(["dpkg", "--search", library], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        package, _, path = line.partition(": ")
        if Path(path).name == library:
            return package.split(":")[0]
    raise SystemExit(f"no Debian package holds the bundled library {library}")


def bundled_libraries(payload: Path) -> list[Path]:
    return sorted(
        path
        for path in payload.rglob("*")
        if path.is_file() and SHARED_LIBRARY.search(path.name) and ".cpython-" not in path.name
    )


def stage_system_notices(root: Path, payload: Path, owned: set[str]) -> None:
    for library in bundled_libraries(payload):
        if library.name in owned or library.name.startswith("libpython"):
            continue
        package = debian_package(library.name)
        folder = root / "licenses" / "debian"
        destination = folder / package / "copyright"
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True)
        shutil.copyfile(Path("/usr/share/doc") / package / "copyright", destination)
        for name in re.findall(
            r"/usr/share/common-licenses/([A-Za-z0-9+-]+(?:\.[0-9]+)*)", destination.read_text(encoding="utf-8")
        ):
            common = folder / "common-licenses" / name
            if not common.exists():
                common.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path("/usr/share/common-licenses") / name, common)


def stage_lgpl_text(root: Path, payload: Path) -> None:
    if any(library.name.startswith("libquadmath") for library in bundled_libraries(payload)):
        shutil.copyfile(ROOT / "notices" / "LGPL-2.1.txt", root / "licenses" / "LGPL-2.1.txt")


def stage_licenses(root: Path, payload: Path, target: str) -> None:
    shutil.copy2(ROOT / "LICENSE", root / "LICENSE")
    owned = stage_distribution_licenses(root)
    stage_python_notices(root, target)
    stage_system_notices(root, payload, owned)
    stage_lgpl_text(root, payload)


def link_stays_inside(root: Path, link: Path) -> bool:
    """Apply the lexical rule of the Meridian installer and the resolved-path rule of Meridian issue #184."""
    target = os.readlink(link)
    if not target or target.startswith("/") or "\\" in target:
        return False
    depth = len(link.relative_to(root).parent.parts)
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        depth = depth - 1 if part == ".." else depth + 1
        if depth < 0:
            return False
    return Path(os.path.realpath(link)).is_relative_to(os.path.realpath(root))


def normalized(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def sums(root: Path) -> bytes:
    """Return ``SHA256SUMS``: each file and link except itself, in byte order of the path."""
    lines = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().encode("utf-8")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if relative == SUMS_NAME or stat.S_ISDIR(mode):
            continue
        if stat.S_ISLNK(mode):
            if not link_stays_inside(root, path):
                raise SystemExit(f"the link {relative} leaves the version directory")
            lines.append(f"link:{os.readlink(path)}  {relative}\n")
        elif stat.S_ISREG(mode):
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
        else:
            raise SystemExit(f"{relative} is not a file, a directory or a link")
    return "".join(lines).encode("utf-8")


def build(version: str, out: Path) -> tuple[Path, str]:
    if SEMVER.fullmatch(version) is None:
        raise SystemExit(f"{version} is not a SemVer version")
    target = host_target()
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        payload = freeze(work)
        root = work / "version"
        shutil.copytree(payload, root / "bin" / PLUGIN_ID, symlinks=True)
        for relative in CONTRACT_FILES:
            destination = root / "contracts" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "contracts" / relative, destination)
        stage_licenses(root, payload, target)
        (root / "plugin.json").write_text(json.dumps(plugin_json(version, target), indent=2) + "\n", encoding="utf-8")
        (root / SUMS_NAME).write_bytes(sums(root))
        out.mkdir(parents=True, exist_ok=True)
        archive = out / f"{PLUGIN_ID}-{version}-{target}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for path in sorted(root.rglob("*")):
                bundle.add(path, arcname=path.relative_to(root).as_posix(), recursive=False, filter=normalized)
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    archive, digest = build(args.version, args.out)
    print(f"{archive} {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
