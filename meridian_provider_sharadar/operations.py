"""The `market-data/v1` operations of the Sharadar plug-in."""

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from meridian_provider_sharadar import credentials
from meridian_provider_sharadar.encoding import (
    calendar_date,
    canonical_json,
    canonical_sha256,
    column,
    decimal_or_null,
    integer_or_null,
    now_timestamp,
    string_or_null,
    utc_timestamp,
)
from meridian_provider_sharadar.identity import sharadar_provider_ticker
from meridian_provider_sharadar.provider import (
    BALANCE_SHEET_MAP,
    CASHFLOW_MAP,
    INCOME_STMT_MAP,
    SharadarProvider,
    SourceUnavailable,
    _build_df,
    _cache_identity,
    _json_value,
    _metadata_is_etf,
    _price_currency_from_metadata,
    _prices_cache_key,
)

logger = logging.getLogger(__name__)

OPERATIONS = ("prices", "statements", "info", "validate", "dividends", "validateCredentials")
MAX_CONCURRENCY = 8
CREDENTIAL_NAME = "api_key"
DEFAULT_PRICE_START = "1990-01-01"
STATEMENT_MAPS = {"income": INCOME_STMT_MAP, "balance": BALANCE_SHEET_MAP, "cashflow": CASHFLOW_MAP}
SOURCE_UNAVAILABLE = "urn:meridian:market-data:source_unavailable"
SOURCE_INVALID = "urn:meridian:market-data:source_invalid"
_TICKER = re.compile(r"[A-Z0-9.\-^]{1,32}", re.ASCII)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_SECRET_NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,63}", re.ASCII)
_TITLES = {
    "credential_missing": "The plug-in has no API key",
    "invalid_http_request": "The HTTP request is not valid",
    "invalid_request": "The request does not match the contract",
    "invalid_ticker": "Sharadar does not support this ticker",
    "method_not_allowed": "The plug-in does not support this HTTP method",
    "source_invalid": "Sharadar returned data that fails a check",
    "source_unavailable": "Sharadar gave no usable answer",
    "unknown_secret": "The plug-in has no credential with this name",
}


class Problem(Exception):
    """An RFC 9457 problem with a stable code and a fixed title."""

    def __init__(self, status: int, code: str, problem_type: str = "about:blank") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.problem_type = problem_type

    def body(self) -> dict:
        return {
            "type": self.problem_type,
            "title": _TITLES.get(self.code, self.code),
            "status": self.status,
            "code": self.code,
        }


@dataclass(frozen=True)
class PluginIdentity:
    id: str
    version: str
    contract_version: str
    contract_revision: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "contract_version": self.contract_version,
            "contract_revision": self.contract_revision,
        }


@dataclass
class Context:
    """The state of one plug-in process: its identity, its key and its cache grant."""

    identity: PluginIdentity
    api_key: str | None
    cache_dir: str
    price_query_locks: dict[str, threading.Lock] = field(default_factory=dict)
    price_query_locks_guard: threading.Lock = field(default_factory=threading.Lock)

    def provider(self, max_age_hours: int) -> SharadarProvider:
        if not self.api_key:
            raise Problem(503, "credential_missing")
        provider = SharadarProvider(api_key=self.api_key, cache_dir=self.cache_dir, cache_ttl_hours=max_age_hours)
        provider._price_query_locks = self.price_query_locks
        provider._price_query_locks_guard = self.price_query_locks_guard
        return provider


def _require_keys(body: object, required: set[str]) -> dict:
    if not isinstance(body, dict) or set(body) != required:
        raise Problem(422, "invalid_request")
    return body


def _ticker(body: dict) -> str:
    value = body["ticker"]
    if not isinstance(value, str) or _TICKER.fullmatch(value) is None:
        raise Problem(422, "invalid_request")
    try:
        sharadar_provider_ticker(value)
    except ValueError:
        raise Problem(422, "invalid_ticker") from None
    return value


def _max_age(body: dict) -> int:
    value = body["max_age_hours"]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8760:
        raise Problem(422, "invalid_request")
    return value


def _boolean(body: dict, key: str) -> bool:
    value = body[key]
    if not isinstance(value, bool):
        raise Problem(422, "invalid_request")
    return value


def _start(body: dict) -> str:
    value = body["start"]
    if value is None:
        return DEFAULT_PRICE_START
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise Problem(422, "invalid_request")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise Problem(422, "invalid_request") from None
    return value


def _observations(capture: dict[str, dict]) -> list[dict]:
    return [
        {
            "source": capture[key]["source"],
            "identity": canonical_json(capture[key]["identity"]),
            "source_fetched_at": utc_timestamp(capture[key]["source_fetched_at"]),
            "sha256": capture[key]["sha256"],
        }
        for key in sorted(capture)
    ]


def _envelope(context: Context, primary: dict, capture: dict[str, dict], data: dict) -> dict:
    identity = _json_value(primary)
    fetched = next(
        (item["source_fetched_at"] for item in capture.values() if item["identity"] == identity),
        None,
    )
    return {
        **data,
        "provenance": {
            "provider_plugin": context.identity.as_dict(),
            "source": f"SHARADAR/{identity['table']}",
            "identity": canonical_json(identity),
            "source_fetched_at": None if fetched is None else utc_timestamp(fetched),
            "retrieved_at": now_timestamp(),
            "sha256": canonical_sha256(data),
        },
        "observations": _observations(capture),
    }


def _source_call(function: Callable[[], dict]) -> dict:
    try:
        return function()
    except SourceUnavailable:
        raise Problem(503, "source_unavailable", SOURCE_UNAVAILABLE) from None
    except ValueError as exc:
        logger.warning("Sharadar data failed a check (%s)", type(exc).__name__)
        raise Problem(502, "source_invalid", SOURCE_INVALID) from None
    except OSError:
        raise Problem(503, "source_unavailable", SOURCE_UNAVAILABLE) from None


def statements(context: Context, body: object) -> dict:
    body = _require_keys(body, {"ticker", "kind", "max_age_hours"})
    ticker, max_age = _ticker(body), _max_age(body)
    mapping = STATEMENT_MAPS.get(body["kind"]) if isinstance(body["kind"], str) else None
    if mapping is None:
        raise Problem(422, "invalid_request")
    provider = context.provider(max_age)

    def run() -> dict:
        with provider.capture_provenance() as capture:
            frame = _build_df(provider._fetch_sf1(ticker, strict=True), mapping)
        rows = [
            [calendar_date(period), *(decimal_or_null(frame.at[label, period]) for label in mapping)]
            for period in frame.columns
        ]
        table = {
            "columns": [column("period_end", "date", None, False)]
            + [column(label, "decimal", None, True) for label in mapping],
            "rows": rows,
        }
        primary = _cache_identity(table="SF1", ticker=ticker, dimension="MRY")
        return _envelope(context, primary, capture, {"table": table})

    return _source_call(run)


def dividends(context: Context, body: object) -> dict:
    body = _require_keys(body, {"ticker", "max_age_hours"})
    ticker, max_age = _ticker(body), _max_age(body)
    provider = context.provider(max_age)

    def run() -> dict:
        with provider.capture_provenance() as capture:
            series = provider.get_dividends(ticker, strict=True)
        table = {
            "columns": [column("period_end", "date", None, False), column("dps", "decimal", None, True)],
            "rows": [[calendar_date(period), decimal_or_null(value)] for period, value in series.items()],
        }
        primary = _cache_identity(table="SF1", ticker=ticker, dimension="MRY")
        return _envelope(context, primary, capture, {"table": table})

    return _source_call(run)


def info(context: Context, body: object) -> dict:
    body = _require_keys(body, {"ticker", "beta", "max_age_hours"})
    ticker, include_beta, max_age = _ticker(body), _boolean(body, "beta"), _max_age(body)
    provider = context.provider(max_age)

    def run() -> dict:
        with provider.capture_provenance() as capture:
            fields = provider.get_ticker_info(ticker, include_beta=include_beta)
        data = {
            name: string_or_null(fields.get(name))
            for name in (
                "shortName",
                "longName",
                "sector",
                "industry",
                "quoteType",
                "exchange",
                "currency",
                "financialCurrency",
            )
        }
        data["is_etf"] = fields.get("quoteType") == "ETF"
        data["sharesOutstanding"] = integer_or_null(fields.get("sharesOutstanding"))
        data["marketCap"] = decimal_or_null(fields.get("marketCap"))
        data["beta"] = decimal_or_null(fields.get("beta"))
        primary = _cache_identity(table="TICKERS", ticker=ticker)
        return _envelope(context, primary, capture, {"info": data})

    return _source_call(run)


def validate(context: Context, body: object) -> dict:
    body = _require_keys(body, {"ticker", "max_age_hours"})
    ticker, max_age = _ticker(body), _max_age(body)
    provider = context.provider(max_age)

    def run() -> dict:
        with provider.capture_provenance() as capture:
            result = provider.validate_ticker(ticker, strict=True)
        data = {"valid": bool(result["valid"]), "name": string_or_null(result["name"])}
        primary = _cache_identity(table="TICKERS", ticker=ticker)
        return _envelope(context, primary, capture, data)

    return _source_call(run)


def prices(context: Context, body: object) -> dict:
    body = _require_keys(body, {"ticker", "start", "force_refresh", "max_age_hours"})
    ticker, start = _ticker(body), _start(body)
    force_refresh, max_age = _boolean(body, "force_refresh"), _max_age(body)
    provider = context.provider(max_age)

    def run() -> dict:
        with provider.capture_provenance() as capture:
            meta = provider._fetch_tickers_meta(ticker, require_price_currency=True, strict=True)
            if meta is None:
                raise ValueError("Sharadar price currency is unavailable")
            currency = _price_currency_from_metadata(meta)
            table_name = "SFP" if _metadata_is_etf(meta) else "SEP"
            frame = provider.price_frame(ticker, table_name, start, force_refresh=force_refresh, strict=True)
        columns = {
            "Open": frame["Open"].tolist(),
            "High": frame["High"].tolist(),
            "Low": frame["Low"].tolist(),
            "Close": frame["Close"].tolist(),
            "Adj Close": frame["Adj Close"].tolist(),
            "Volume": frame["Volume"].tolist(),
        }
        rows = [
            [calendar_date(day), *(decimal_or_null(columns[name][index]) for name in columns)]
            for index, day in enumerate(frame.index)
        ]
        table = {
            "columns": [
                column("Date", "date", None, False),
                column("Open", "decimal", currency, True),
                column("High", "decimal", currency, True),
                column("Low", "decimal", currency, True),
                column("Close", "decimal", currency, False),
                column("Adj Close", "decimal", currency, False),
                column("Volume", "decimal", None, True),
            ],
            "rows": rows,
        }
        _key, normalized_start = _prices_cache_key(ticker, table_name, start)
        primary = _cache_identity(table=table_name, ticker=ticker, start=normalized_start)
        return _envelope(context, primary, capture, {"price_currency": currency, "table": table})

    return _source_call(run)


def validate_credentials(context: Context, body: object) -> dict:
    body = _require_keys(body, {"secret_name", "candidate"})
    name, candidate = body["secret_name"], body["candidate"]
    if not isinstance(name, str) or _SECRET_NAME.fullmatch(name) is None:
        raise Problem(422, "invalid_request")
    if name != CREDENTIAL_NAME:
        raise Problem(422, "unknown_secret")
    if not isinstance(candidate, str) or not 1 <= len(candidate) <= 4096 or not candidate.strip():
        raise Problem(422, "invalid_request")
    try:
        result = credentials.check_candidate(candidate.strip())
    except credentials.CredentialCheckUnavailable:
        raise Problem(503, "source_unavailable", SOURCE_UNAVAILABLE) from None
    return {"valid": result.valid, "code": result.code}


HANDLERS: dict[str, Callable[[Context, object], dict]] = {
    "prices": prices,
    "statements": statements,
    "info": info,
    "validate": validate,
    "dividends": dividends,
    "validateCredentials": validate_credentials,
}
