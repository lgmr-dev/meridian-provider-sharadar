"""Install a release archive with the Meridian CLI and start it with the Meridian host of ``tests/reference``.

Run from the repository root: ``python -m release.host_acceptance <archive>``. The proof removes
``SHARADAR_API_KEY`` from its environment, and no request reaches a data operation with a key, so
the plug-in makes no Sharadar call.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "tests" / "reference"
PLUGIN_ID = "meridian-provider-sharadar"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"host acceptance failed: {message}")


def accept(archive: Path) -> dict:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as scratch:
        registry = Path(scratch) / "plugins" / "registry.json"
        data_dir = Path(scratch) / "data"
        data_dir.mkdir()
        os.environ.pop("SHARADAR_API_KEY", None)
        environment = {**os.environ, "MERIDIAN_PLUGIN_REGISTRY": str(registry), "DATA_DIR": str(data_dir)}
        installed = subprocess.run(
            [
                sys.executable,
                str(REFERENCE / "desktop" / "fixture_plugin" / "pty_run.py"),
                sys.executable,
                "-m",
                "server.plugins",
                "install",
                str(archive),
                "--sha256",
                digest,
            ],
            cwd=REFERENCE,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
        )
        check(installed.returncode == 0, f"the install command failed: {(installed.stdout + installed.stderr)[-2000:]}")
        os.environ["MERIDIAN_PLUGIN_REGISTRY"] = str(registry)
        sys.path.insert(0, str(REFERENCE))
        from server.plugins.client import PluginProblem, request_json
        from server.plugins.host import PluginHost

        host = PluginHost(data_dir=lambda: str(data_dir))
        try:
            check(host.ensure_started(PLUGIN_ID) == "ready", "the host did not start the plug-in")
            status = next(item for item in host.status() if item["id"] == PLUGIN_ID)
            check(status["state"] == "ready", f"the plug-in state is {status['state']}")
            try:
                host.call(PLUGIN_ID, "validate", {"ticker": "SYNTH", "max_age_hours": 168})
            except PluginProblem as exc:
                check(exc.code == "credential_missing", f"validate gave the problem {exc.code}")
            else:
                check(False, "validate without a key gave a result")
        finally:
            host.stop_all()
        (data_dir / ".secrets.json").write_text(json.dumps({"sharadar_api_key": "not-a-real-key"}), encoding="utf-8")
        keyed = PluginHost(data_dir=lambda: str(data_dir))
        try:
            check(keyed.ensure_started(PLUGIN_ID) == "ready", "the host did not start the plug-in with a key")
            running = keyed._slots[PLUGIN_ID].running
            health = request_json(running.endpoint, "GET", "/v1/health", None, timeout=30)
            check(health == {"status": "ok", "tools": []}, "the host did not send the stored key")
        finally:
            keyed.stop_all()
        entry = json.loads(registry.read_text(encoding="utf-8"))["plugins"][0]
    return {"archive": archive.name, "state": status["state"], "binding": entry["binding"], "grants": entry["grants"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(accept(args.archive.resolve())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
