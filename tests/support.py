import json
import secrets
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from meridian_provider_sharadar import server
from meridian_provider_sharadar.operations import Context, PluginIdentity

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
MARKET = "market-data/v1/openapi.json"
RUNTIME = "plugin-runtime/v1/openapi.json"
SYNTHETIC_KEY = "not-a-real-key"
CONTRACT_BASE = "https://contracts.invalid/"


def contract_revision() -> str:
    return json.loads((CONTRACTS / "market-data" / "v1" / "contract.json").read_text(encoding="utf-8"))["revision"]


def identity() -> PluginIdentity:
    return PluginIdentity(
        id="meridian-provider-sharadar",
        version="1.0.0-rc.1",
        contract_version="1.0.0",
        contract_revision=contract_revision(),
    )


def write_plugin_json(directory: Path) -> Path:
    plugin = {
        **identity().as_dict(),
        "family": "market-data",
        "target": "x86_64-unknown-linux-gnu",
        "binding": {"provider_id": "sharadar"},
        "command": ["bin/meridian-provider-sharadar/meridian-provider-sharadar", "plugin.json"],
        "grants": ["plugin_cache"],
        "credentials": {"api_key": {"secret": "sharadar_api_key", "env": "SHARADAR_API_KEY"}},
        "external_tools": [],
    }
    path = directory / "plugin.json"
    path.write_text(json.dumps(plugin, indent=2) + "\n", encoding="utf-8")
    return path


def handshake(cache_dir: Path, lock_path: Path, api_key: str | None = SYNTHETIC_KEY) -> dict:
    return {
        "protocol_version": 1,
        "token": secrets.token_hex(32),
        "grants": {"plugin_cache": str(cache_dir)},
        "secrets": {} if api_key is None else {"api_key": api_key},
        "locale": "en",
        "lock_path": str(lock_path),
    }


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

    def request(self, method: str, path: str, body: object = None, headers: dict | None = None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"Authorization": f"Bearer {self.token}"}
        sent.update(headers or {})
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            sent["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), json.loads(payload) if payload else None

    def call(self, operation: str, body: dict):
        status, headers, payload = self.request("POST", f"/v1/{operation}", body)
        return status, payload


@contextmanager
def running_plugin(cache_dir: Path, api_key: str | None = SYNTHETIC_KEY):
    """Run the plug-in HTTP server in this process, so that tests can replace the Nasdaq transport."""
    token = secrets.token_hex(32)
    context = Context(identity=identity(), api_key=api_key, cache_dir=str(cache_dir))
    port: dict = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(context, token, port))
    httpd.daemon_threads = True
    port["value"] = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(port["value"], token)
    finally:
        httpd.shutdown()
        httpd.server_close()


def start_process(plugin_json: Path, line: dict, extra_env: dict | None = None) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "meridian_provider_sharadar", str(plugin_json)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(plugin_json.parent), **(extra_env or {})},
    )
    process.stdin.write(json.dumps(line).encode("utf-8") + b"\n")
    process.stdin.flush()
    return process


def contract_validator(schema_ref: str) -> Draft202012Validator:
    """Return a validator for one schema of the pinned contracts, for example ``market-data/v1/openapi.json#/...``."""
    registry = Registry().with_resources(
        (CONTRACT_BASE + name, DRAFT202012.create_resource(json.loads((CONTRACTS / name).read_text(encoding="utf-8"))))
        for name in (MARKET, RUNTIME)
    )
    return Draft202012Validator({"$ref": CONTRACT_BASE + schema_ref}, registry=registry)


def _make_sf1_rows() -> list[dict]:
    rows = []
    for index, year in enumerate((2024, 2023, 2022)):
        scale = 1 + index / 10
        rows.append(
            {
                "reportperiod": f"{year}-12-31",
                "calendardate": f"{year}-12-31",
                "datekey": f"{year + 1}-02-15",
                "revenue": 1_200_000_000 / scale,
                "netinc": 120_000_000.25 / scale,
                "netinccmn": 120_000_000,
                "gp": 500_000_000 / scale,
                "opinc": 240_000_000 / scale,
                "ebitda": 294_000_000.1 / scale,
                "ebit": 240_000_000,
                "eps": 5.0 / scale,
                "epsdil": None if year == 2024 else 4.8 / scale,
                "shareswa": 25_000_000,
                "shareswadil": 26_000_000,
                "taxexp": 36_000_000,
                "ebt": 228_000_000,
                "assets": 2e16,
                "equity": 900_000_000,
                "debt": 460_000_000,
                "debtnc": 360_000_000,
                "assetsc": 640_000_000,
                "liabilitiesc": 280_000_000,
                "cashneq": 170_000_000,
                "sharesbas": 25_000_000,
                "investmentsc": 1e-05,
                "goodwill": 100_000_000,
                "intangibles": 50_000_000,
                "ncfo": 270_000_000,
                "capex": -90_000_000.5,
                "fcf": 180_000_000,
                "depamor": 54_000_000,
                "ncfdiv": None if year == 2022 else -50_000_000.0 / scale,
                "ncfdebt": 20_000_000,
                "price": 120.0 / scale,
            }
        )
    return rows


class FakeTables:
    """Offline Nasdaq Data Link tables with synthetic data. ``fail`` names tables that raise."""

    def __init__(self, fail: set[str] | None = None, empty: set[str] | None = None, tickers: dict | None = None):
        self.fail = fail or set()
        self.empty = empty or set()
        self.tickers = tickers or {}
        self.calls: list[tuple[str, str, dict]] = []
        self._lock = threading.Lock()

    def get_table(self, table: str, *, ticker: str, **query) -> pd.DataFrame:
        with self._lock:
            self.calls.append((table, ticker, query))
        name = table.removeprefix("SHARADAR/")
        if name in self.fail:
            raise ConnectionError("synthetic transport failure")
        if name in self.empty:
            return pd.DataFrame()
        if name == "TICKERS":
            return pd.DataFrame([self.ticker_row(ticker)])
        if name == "SF1":
            return pd.DataFrame([{"ticker": ticker, "dimension": "MRY", **row} for row in _make_sf1_rows()][::-1])
        return self.price_frame(ticker, query)

    def ticker_row(self, ticker: str) -> dict:
        fund = ticker == "SPY"
        row = {
            "ticker": ticker,
            "name": f"{ticker} Synthetic Holdings",
            "sector": None if fund else "Technology",
            "sicindustry": None if fund else "Services-Prepackaged Software",
            "category": "ETF" if fund else "Domestic Common Stock",
            "exchange": "NYSEARCA" if fund else "NASDAQ",
            "currency": "USD",
            "table": "SFP" if fund else "SEP",
            "isdelisted": "N",
        }
        row.update(self.tickers.get(ticker, {}))
        return row

    @staticmethod
    def price_frame(ticker: str, query: dict) -> pd.DataFrame:
        seed = sum(map(ord, ticker))
        end = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() - pd.Timedelta(days=1)
        dates = pd.bdate_range(pd.Timestamp(query["date"]["gte"]), end)[-1700:]
        closes = 50 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0.0004, 0.01, len(dates))))
        if ticker == "SPY":
            volume = np.arange(len(dates), dtype=np.int64) * 1000 + 5_000_000
        else:
            volume = np.arange(len(dates), dtype=float) * 1000.5 + 9_000_000_000.25
            volume[1] = np.nan
        frame = pd.DataFrame(
            {
                "ticker": ticker,
                "date": dates,
                "open": closes * 1.001,
                "high": closes * 1.01,
                "low": closes * 0.99,
                "close": closes,
                "closeadj": closes * 0.98,
                "volume": volume,
            }
        )
        frame.loc[0, "open"] = 0.00001
        return frame.iloc[::-1].reset_index(drop=True)
