"""Start a release archive as the Meridian host does, and prove the runtime rules and the data operations.

Run from the repository root: ``python -m release.launch_proof <archive>``. The proof makes no
live Sharadar call: the data operations read a synthetic raw cache, and each other request goes to
a proxy on a loopback port of the proof that closes each connection at once.
"""

import argparse
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path

from meridian_provider_sharadar.provider import _cache_identity, _prices_cache_key, _write_cache
from release.build_archive import SUMS_NAME, sums

READY_SECONDS = 30
TICKER = "SYNTH"
START = "2024-01-02"
OPERATIONS = ["prices", "statements", "info", "validate", "dividends", "validateCredentials"]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"launch proof failed: {message}")


def extract(archive: Path, home: Path) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        plugin = json.loads(bundle.extractfile("plugin.json").read())
        root = home / plugin["id"] / plugin["version"]
        root.mkdir(parents=True)
        bundle.extractall(root, filter="data")
    check((root / SUMS_NAME).read_bytes() == sums(root), "the extracted files differ from SHA256SUMS")
    return root


def seed_cache(cache: Path, fetched_at: str) -> None:
    rows = [
        {
            "ticker": TICKER,
            "dimension": "MRY",
            "reportperiod": f"{year}-12-31",
            "datekey": f"{year + 1}-02-15",
            "revenue": 1000.25 * (year - 2019),
            "netinc": 100.5,
            "shareswa": 10.0,
            "sharesbas": 10.0,
            "ncfdiv": -5.0,
            "price": 12.5,
        }
        for year in (2024, 2023, 2022)
    ]
    _write_cache(
        str(cache),
        f"{TICKER}_sf1",
        {"rows": rows},
        identity=_cache_identity(table="SF1", ticker=TICKER, dimension="MRY"),
        source_fetched_at=fetched_at,
    )
    meta = {
        "ticker": TICKER,
        "name": "Synthetic Equity Corp",
        "category": "Domestic Common Stock",
        "exchange": "NASDAQ",
        "currency": "USD",
        "table": "SEP",
        "isdelisted": "N",
    }
    _write_cache(
        str(cache),
        f"{TICKER}_meta",
        {"meta": meta},
        identity=_cache_identity(table="TICKERS", ticker=TICKER),
        source_fetched_at=fetched_at,
    )
    key, start = _prices_cache_key(TICKER, "SEP", START)
    prices = [
        {"ticker": TICKER, "date": day, "open": 10.0, "close": 10.5, "closeadj": 10.25, "volume": 1000}
        for day in ("2024-01-02", "2024-01-03")
    ]
    _write_cache(
        str(cache),
        key,
        {"rows": prices},
        identity=_cache_identity(table="SEP", ticker=TICKER, start=start),
        source_fetched_at=fetched_at,
    )


def start(
    root: Path, plugin: dict, home: Path, api_key: str | None, proxy: str, children: list[subprocess.Popen]
) -> tuple[subprocess.Popen, str, dict, float]:
    token = secrets.token_hex(32)
    handshake = {
        "protocol_version": 1,
        "token": token,
        "grants": {"plugin_cache": str(home / plugin["id"] / "cache")},
        "secrets": {} if api_key is None else {"api_key": api_key},
        "locale": "en",
        "lock_path": str(home / plugin["id"] / "plugin.lock"),
    }
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    environment |= {name: proxy for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}
    began = time.monotonic()
    process = subprocess.Popen(
        [str(root / plugin["command"][0]), *plugin["command"][1:]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=root,
        env=environment,
    )
    children.append(process)
    threading.Thread(target=process.stderr.read, daemon=True).start()
    process.stdin.write(json.dumps(handshake).encode("utf-8") + b"\n")
    process.stdin.flush()
    line = read_line(process.stdout.fileno(), began + READY_SECONDS)
    return process, token, json.loads(line), time.monotonic() - began


def read_line(descriptor: int, deadline: float) -> bytes:
    line = b""
    while not line.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        check(remaining > 0, f"no ready line in {READY_SECONDS} seconds")
        readable, _, _ = select.select([descriptor], [], [], remaining)
        check(bool(readable), f"no ready line in {READY_SECONDS} seconds")
        chunk = os.read(descriptor, 1)
        check(chunk != b"", "the plug-in closed stdout before its ready line")
        line += chunk
    check(time.monotonic() <= deadline, f"no ready line in {READY_SECONDS} seconds")
    return line


def request(port: int, token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=120)
    headers = {"Authorization": f"Bearer {token}"}
    data = None if body is None else json.dumps(body).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def stop(process: subprocess.Popen) -> bytes:
    process.stdin.close()
    process.wait(timeout=10)
    stdout = process.stdout.read()
    process.stdout.close()
    return stdout


def close_each_connection(dead: socket.socket, closing: threading.Event) -> None:
    """Accept and close each connection, so that a request through the proxy fails at once on each platform."""
    while not closing.is_set():
        try:
            connection, _address = dead.accept()
        except OSError:
            return
        connection.close()


def stop_closing(dead: socket.socket, closing: threading.Event, closer: threading.Thread) -> None:
    """Stop the proxy thread with one connection after the stop flag, which works on Linux and macOS."""
    closing.set()
    try:
        socket.create_connection(dead.getsockname(), timeout=5).close()
    except OSError:
        pass
    closer.join(timeout=10)


def reap(children: list[subprocess.Popen]) -> None:
    for process in children:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout):
            if not stream.closed:
                stream.close()


def value(payload: dict, column: str, row: int = 0) -> object:
    names = [item["name"] for item in payload["table"]["columns"]]
    return payload["table"]["rows"][row][names.index(column)]


def prove(archive: Path) -> dict:
    children: list[subprocess.Popen] = []
    with tempfile.TemporaryDirectory() as scratch, socket.socket() as dead:
        dead.bind(("127.0.0.1", 0))
        dead.listen()
        closing = threading.Event()
        closer = threading.Thread(target=close_each_connection, args=(dead, closing), daemon=True)
        closer.start()
        try:
            proxy = f"http://127.0.0.1:{dead.getsockname()[1]}"
            home = Path(scratch)
            root = extract(archive, home)
            plugin = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
            fetched_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            seed_cache(home / plugin["id"] / "cache", fetched_at)
            process, token, ready, seconds = start(root, plugin, home, "not-a-real-key", proxy, children)
            port = ready["port"]
            status, manifest = request(port, token, "GET", "/v1/manifest")
            check(status == 200, "the manifest request failed")
            for key in ("id", "family", "version", "contract_version", "contract_revision"):
                check(manifest[key] == plugin[key], f"the manifest {key} differs from plugin.json")
            check(manifest["capabilities"] == {"operations": OPERATIONS, "max_concurrency": 8}, "capabilities")
            check(request(port, token, "GET", "/v1/health") == (200, {"status": "ok", "tools": []}), "health")
            check(request(port, "0" * 64, "GET", "/v1/manifest")[0] == 401, "a wrong token was accepted")
            expected_time = fetched_at.removesuffix("+00:00") + "Z"
            results = {}
            for operation, body in {
                "statements": {"ticker": TICKER, "kind": "income", "max_age_hours": 8760},
                "dividends": {"ticker": TICKER, "max_age_hours": 8760},
                "validate": {"ticker": TICKER, "max_age_hours": 8760},
                "info": {"ticker": TICKER, "beta": False, "max_age_hours": 8760},
                "prices": {"ticker": TICKER, "start": START, "force_refresh": False, "max_age_hours": 8760},
            }.items():
                status, payload = request(port, token, "POST", f"/v1/{operation}", body)
                check(status == 200, f"{operation} gave HTTP {status}: {payload.get('code')}")
                check(
                    payload["provenance"]["source_fetched_at"] == expected_time, f"{operation} did not read the cache"
                )
                results[operation] = payload
            check(value(results["statements"], "Total Revenue") == "5001.25", "statements value")
            check(value(results["dividends"], "dps", -1) == "0.5", "dividends value")
            check(
                results["validate"]["valid"] is True and results["validate"]["name"] == "Synthetic Equity Corp",
                "validate",
            )
            check(results["info"]["info"]["marketCap"] == "125.0", "info value")
            check(
                results["prices"]["price_currency"] == "USD" and value(results["prices"], "Close") == "10.5", "prices"
            )
            status, payload = request(
                port,
                token,
                "POST",
                "/v1/validateCredentials",
                {"secret_name": "api_key", "candidate": "not-a-real-key"},
            )
            check((status, payload["code"]) == (503, "source_unavailable"), "validateCredentials reached a network")
            second, _token, held, _seconds = start(root, plugin, home, None, proxy, children)
            check(held == {"error": "lock_held"}, "a second process did not get lock_held")
            check(second.wait(timeout=10) == 0, "the second process did not exit")
            stdout = stop(process)
            check(process.returncode == 0, "the plug-in did not exit on stdin end of file")
            check(stdout == b"", "the plug-in wrote to stdout after its ready line")
        finally:
            try:
                reap(children)
            finally:
                stop_closing(dead, closing, closer)
    return {"archive": archive.name, "target": plugin["target"], "ready_seconds": round(seconds, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(prove(args.archive.resolve())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
