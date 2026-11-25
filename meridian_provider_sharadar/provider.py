"""Sharadar (Nasdaq Data Link) data provider.

Fetches fundamentals (SF1), equity prices (SEP), fund prices (SFP),
and metadata (TICKERS) from Sharadar, returning data in yfinance-compatible
formats so existing code works with either data source.

Requires: nasdaq-data-link >= 1.0.4
API key: Set SHARADAR_API_KEY environment variable or pass to SharadarProvider.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from meridian_provider_sharadar.identity import sharadar_provider_ticker, validated_security_name

logger = logging.getLogger(__name__)

_CACHE_SCHEMA_VERSION = 3
_CACHE_FUTURE_TOLERANCE = timedelta(minutes=5)
# Every reader of a SEP/SFP price-window cache uses this TTL.
_PRICE_CACHE_TTL_HOURS = 24
_BETA_BENCHMARK = "SPY"
_BETA_MIN_RETURNS = 24
_BETA_LOOKBACK_YEARS = 5
SHARADAR_HTTP_TIMEOUT = (5, 30)
_TRANSPORT_LOCK = threading.Lock()
_TRANSPORT_STATE = threading.local()


INCOME_STMT_MAP = {
    "Total Revenue": "revenue",
    "Net Income": "netinc",
    "Net Income Common Stockholders": "netinccmn",
    "Gross Profit": "gp",
    "Operating Income": "opinc",
    "EBITDA": "ebitda",
    "EBIT": "ebit",
    "Basic EPS": "eps",
    "Diluted EPS": "epsdil",
    "Basic Average Shares": "shareswa",
    "Diluted Average Shares": "shareswadil",
    "Tax Provision": "taxexp",
    "Pretax Income": "ebt",
}

BALANCE_SHEET_MAP = {
    "Total Assets": "assets",
    "Stockholders Equity": "equity",
    "Total Debt": "debt",
    "Long Term Debt": "debtnc",
    "Current Assets": "assetsc",
    "Current Liabilities": "liabilitiesc",
    "Cash And Cash Equivalents": "cashneq",
    "Cash Cash Equivalents And Short Term Investments": "cashneq",
    "Ordinary Shares Number": "sharesbas",
    "Other Short Term Investments": "investmentsc",
    "Goodwill": "goodwill",
    "Intangible Assets": "intangibles",
}

# NOTE: Sharadar SF1 has no direct "Change In Working Capital" cash flow field.
# The valuation engine requires the two complete balance-sheet periods and
# computes delta_WC from Current Assets / Current Liabilities when the explicit
# cash-flow row is absent. An observed zero remains an observed zero.
# SF1 ncfdebt = combined ST+LT debt issuance. Map it to an explicit aggregate
# key so the valuation layer never mistakes it for long-term-only issuance.
CASHFLOW_MAP = {
    "Operating Cash Flow": "ncfo",
    "Total Cash From Operating Activities": "ncfo",
    "Capital Expenditure": "capex",
    "Free Cash Flow": "fcf",
    "Depreciation And Amortization": "depamor",
    "Depreciation Amortization Depletion": "depamor",
    "Cash Dividends Paid": "ncfdiv",
    "Net Debt Issuance": "ncfdebt",
}

_CATEGORY_TO_QUOTE_TYPE = {
    "etf": "ETF",
    "domestic common stock": "EQUITY",
    "domestic common stock primary class": "EQUITY",
    "domestic common stock secondary class": "EQUITY",
    "adr": "EQUITY",
}


class _SharadarMetadataConflict(ValueError):
    """TICKERS category and price-table identity disagree."""


class SourceUnavailable(Exception):
    """A Sharadar query failed where the in-tree provider returned an empty result."""


class SourceInvalid(ValueError):
    """A Sharadar response failed a provider check where the in-tree provider returned an empty result."""


def _strict_failure(exc: Exception, *, queried: bool, query: str) -> Exception:
    """Classify a caught failure: before an HTTP answer it is unavailable, after an answer it is invalid.

    ``queried`` is true after ``get_table`` returned. The transport wrapper of
    ``_init_api`` records each HTTP answer, so a failure in the client after an
    answer, for example a body or a row that the client cannot parse, is invalid.
    A ``ValueError`` after an answer is invalid also when it is an ``OSError``,
    as the ``JSONDecodeError`` of Requests is. Another ``OSError`` after an
    answer is a local file error and is unavailable.
    """
    answered = queried or getattr(_TRANSPORT_STATE, "answered", False)
    if not answered:
        return SourceUnavailable(f"Sharadar {query} query failed")
    if isinstance(exc, ValueError):
        return exc
    if isinstance(exc, OSError):
        return SourceUnavailable(f"Sharadar {query} cache access failed")
    return SourceInvalid(f"Sharadar {query} response failed a check")


def _metadata_is_etf(meta: dict) -> bool:
    """Return provider-backed fund identity from TICKERS category/table metadata."""
    category = (meta.get("category") or "").lower()
    table = (meta.get("table") or "").upper()
    category_quote_type = _CATEGORY_TO_QUOTE_TYPE.get(category)
    table_quote_type = {"SEP": "EQUITY", "SFP": "ETF"}.get(table)
    if category_quote_type is not None and table_quote_type is not None and category_quote_type != table_quote_type:
        raise _SharadarMetadataConflict("Sharadar TICKERS category and table identify conflicting asset types")
    return "etf" in category or table == "SFP"


def _cache_path(cache_dir: str, key: str) -> Path:
    """Return a contained path for one validated Sharadar cache key."""
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9^][A-Za-z0-9^_.-]{0,199}", key) is None:
        raise ValueError("Invalid Sharadar cache key")
    cache_root = (Path(cache_dir) / "cache" / "sharadar").resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    path = (cache_root / f"{key.upper()}.json").resolve()
    if path.parent != cache_root:
        raise ValueError("Sharadar cache path escapes the configured cache directory")
    return path


def _json_value(value):
    """Return the exact JSON representation used for hashing and storage."""
    return json.loads(json.dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _records_from_frame(frame: pd.DataFrame) -> list[dict]:
    """Convert one provider frame to strict JSON-compatible records.

    Nasdaq Data Link represents unavailable table cells as pandas ``NaN`` or
    ``NaT``. Those are missing observations, so normalize them to JSON ``null``
    at the provider boundary while leaving infinities for the strict cache
    serializer to reject.
    """
    object_frame = frame.astype(object)
    normalized = object_frame.where(pd.notna(object_frame), None)
    return normalized.to_dict("records")


def _payload_sha256(payload) -> str:
    encoded = json.dumps(
        _json_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_identity(*, table: str, ticker: str, **query) -> dict:
    """Build the canonical identity of one exact Sharadar query."""
    return {
        "provider": "sharadar",
        "table": table.upper(),
        "ticker": _bare_ticker(ticker).upper(),
        **{key: value for key, value in sorted(query.items())},
    }


def _read_cache(cache_dir: str, key: str, ttl_hours: int, *, identity: dict) -> dict | None:
    """Read a fresh, query-bound, integrity-checked Sharadar cache envelope."""
    path = _cache_path(cache_dir, key)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if envelope.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return None
        if envelope.get("identity") != _json_value(identity):
            return None
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or envelope.get("sha256") != _payload_sha256(payload):
            return None
        fetched = datetime.fromisoformat(envelope.get("source_fetched_at", ""))
        if fetched.tzinfo is None:
            return None
        age = datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)
        if age < -_CACHE_FUTURE_TOLERANCE or age > timedelta(hours=ttl_hours):
            return None
        return envelope
    except Exception:
        return None


def _write_cache(
    cache_dir: str,
    key: str,
    payload: dict,
    *,
    identity: dict,
    source_fetched_at: str | None = None,
) -> dict:
    """Atomically write and return a versioned Sharadar cache envelope."""
    path = _cache_path(cache_dir, key)
    normalized_payload = _json_value(payload)
    fetched_at = source_fetched_at or datetime.now(timezone.utc).isoformat()
    envelope = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "identity": _json_value(identity),
        "source_fetched_at": fetched_at,
        "sha256": _payload_sha256(normalized_payload),
        "payload": normalized_payload,
    }
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return envelope


def _bare_ticker(ticker: str) -> str:
    """Return the exact validated Sharadar symbol for a Stock Selection ticker."""
    return sharadar_provider_ticker(ticker)


def _prices_cache_key(ticker: str, table: str, start: str) -> tuple[str, str]:
    """Return a query-complete price-cache key and normalized start date."""
    try:
        normalized_start = pd.Timestamp(start).date().isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Sharadar price-history start date: {start!r}") from exc
    compact_start = normalized_start.replace("-", "")
    return f"{_bare_ticker(ticker)}_prices_{table.lower()}_{compact_start}", normalized_start


# The windows of one ticker and price table are one family. A window
# replacement and the cleanup of that family run one at a time in this
# process, so the cleanup cannot delete a window that another writer has
# just replaced. The lock is never held during a network request.
_PRICE_WINDOW_FAMILY_LOCKS: dict[str, threading.Lock] = {}
_PRICE_WINDOW_FAMILY_LOCKS_GUARD = threading.Lock()


def _price_window_family_lock(cache_dir: str, key: str) -> threading.Lock:
    """Return the process-wide lock of the price-window family of ``key``."""
    family = str(_cache_path(cache_dir, key).parent / key.rsplit("_", 1)[0].upper())
    with _PRICE_WINDOW_FAMILY_LOCKS_GUARD:
        return _PRICE_WINDOW_FAMILY_LOCKS.setdefault(family, threading.Lock())


def _prune_expired_price_windows(cache_dir: str, keep_key: str) -> None:
    """Delete the other price windows of one ticker and table that no read can use.

    The query start of most price windows comes from the request date, so each
    day writes new files. A read accepts a window for _PRICE_CACHE_TTL_HOURS
    after its envelope ``source_fetched_at``, so an older window is never read
    again. The file modification time is only a fast filter: a write occurs
    after the envelope gets its ``source_fetched_at``. A file that cannot be
    parsed stays. A file that another writer removed is not an error.
    """
    keep = _cache_path(cache_dir, keep_key)
    window = re.compile(re.escape(keep_key.rsplit("_", 1)[0].upper()) + r"_\d{8}\.json")
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=_PRICE_CACHE_TTL_HOURS)
    candidates = []
    with os.scandir(keep.parent) as entries:
        for entry in entries:
            if entry.name == keep.name or window.fullmatch(entry.name) is None:
                continue
            try:
                modified = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
            except FileNotFoundError:
                continue
            if now - modified > max_age:
                candidates.append(entry.name.removesuffix(".json"))
    with _price_window_family_lock(cache_dir, keep_key):
        for key in candidates:
            path = _cache_path(cache_dir, key)
            try:
                fetched = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["source_fetched_at"])
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if fetched.tzinfo is not None and now - fetched > max_age:
                path.unlink(missing_ok=True)


def _validate_exact_ticker(rows: list[dict], ticker: str, *, context: str) -> None:
    expected = _bare_ticker(ticker).upper()
    if any(str(row.get("ticker", "")).upper() != expected for row in rows):
        raise ValueError(f"{context}: response ticker does not match the requested ticker")


def _validate_sf1_rows(rows: list[dict], ticker: str) -> None:
    _validate_exact_ticker(rows, ticker, context="Sharadar SF1")
    if any(str(row.get("dimension", "")).upper() != "MRY" for row in rows):
        raise ValueError("Sharadar SF1: response dimension is not MRY")
    fiscal_periods = []
    future_limit = pd.Timestamp(datetime.now(timezone.utc) + _CACHE_FUTURE_TOLERANCE).tz_localize(None)
    for row in rows:
        value = row.get("reportperiod")
        if not value:
            raise ValueError("Sharadar SF1: response row has no fiscal report period")
        try:
            period = pd.Timestamp(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Sharadar SF1: response row has an invalid fiscal report period") from exc
        if pd.isna(period):
            raise ValueError("Sharadar SF1: response row has an invalid fiscal report period")
        if period.tzinfo is not None:
            period = period.tz_convert("UTC").tz_localize(None)
        if period > future_limit:
            raise ValueError("Sharadar SF1: fiscal report period is in the future")
        filing_value = row.get("datekey")
        if filing_value:
            try:
                filing_date = pd.Timestamp(filing_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Sharadar SF1: response row has an invalid filing date") from exc
            if pd.isna(filing_date):
                raise ValueError("Sharadar SF1: response row has an invalid filing date")
            if filing_date.tzinfo is not None:
                filing_date = filing_date.tz_convert("UTC").tz_localize(None)
            if filing_date > future_limit:
                raise ValueError("Sharadar SF1: filing date is in the future")
        fiscal_periods.append(period.normalize())
    if len(set(fiscal_periods)) != len(fiscal_periods):
        raise ValueError("Sharadar SF1: duplicate fiscal report periods")
    if any(current >= previous for previous, current in zip(fiscal_periods, fiscal_periods[1:])):
        raise ValueError("Sharadar SF1: fiscal report periods are not newest-first")


def _validate_meta(meta: dict, ticker: str) -> None:
    _validate_exact_ticker([meta], ticker, context="Sharadar TICKERS")
    validated_security_name(meta.get("name"), _bare_ticker(ticker))
    _metadata_is_etf(meta)


def _price_currency_from_metadata(meta: dict) -> str:
    """Read quote currency only from a SEP/SFP TICKERS row, never SF1.

    SHARADAR/INDICATORS defines TICKERS.currency as reporting currency for
    SF1, but EOD price currency for SEP/SFP. A legacy table-less row cannot
    establish that distinction and must be refreshed before price publication.
    """
    currency = meta.get("currency")
    if str(meta.get("table", "")).upper() not in {"SEP", "SFP"}:
        raise ValueError("Sharadar price currency requires SEP/SFP metadata")
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency.strip().upper()) is None:
        raise ValueError("Sharadar price currency is missing or invalid")
    return currency.strip().upper()


def _select_price_metadata(rows: list[dict], ticker: str) -> dict:
    """Select the unique SEP/SFP security row from TICKERS metadata.

    TICKERS can return the same security once per subscribed Sharadar product
    (for example SF2, SF1, and SEP). Stock Selection consumes the price-product
    row because SEP/SFP is the authoritative stock/fund routing identity.
    Legacy single-row responses remain valid for additive cache compatibility.
    """
    _validate_exact_ticker(rows, ticker, context="Sharadar TICKERS")
    if len(rows) == 1:
        meta = rows[0]
    else:
        price_rows = [row for row in rows if str(row.get("table", "")).upper() in {"SEP", "SFP"}]
        if len(price_rows) != 1:
            raise ValueError("Sharadar TICKERS: response must contain exactly one SEP/SFP metadata row")
        meta = price_rows[0]
    _validate_meta(meta, ticker)
    return meta


def _validate_price_rows(rows: list[dict], ticker: str, start: str, table: str) -> None:
    _validate_exact_ticker(rows, ticker, context=f"Sharadar {table}")
    dates = []
    lower_bound = pd.Timestamp(start)
    upper_bound = pd.Timestamp(datetime.now(timezone.utc) + _CACHE_FUTURE_TOLERANCE).tz_localize(None)
    for row in rows:
        if "date" not in row:
            raise ValueError(f"Sharadar {table}: response row has no price date")
        if "close" not in row:
            raise ValueError(f"Sharadar {table}: no split-adjusted price-only close data")
        if "closeadj" not in row:
            raise ValueError(
                "Sharadar adjusted close is unavailable; annual SF1 dividends cannot reconstruct "
                "exact ex-dividend history"
            )
        try:
            date = pd.Timestamp(row["date"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Sharadar {table}: response row has an invalid price date") from exc
        if pd.isna(date):
            raise ValueError(f"Sharadar {table}: response row has an invalid price date")
        if date.tzinfo is not None:
            date = date.tz_convert(None)
        if date < lower_bound or date > upper_bound:
            raise ValueError(f"Sharadar {table}: response date is outside the requested window")
        for field, label in (
            ("close", "price-only close"),
            ("closeadj", "adjusted close"),
        ):
            try:
                value = float(row[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Sharadar {table}: incomplete {label} data") from exc
            if not np.isfinite(value):
                raise ValueError(f"Sharadar {table}: incomplete {label} data")
            if value <= 0:
                raise ValueError(f"Sharadar {table}: non-positive {label} data")
        dates.append(date)
    if len(set(dates)) != len(dates):
        raise ValueError(f"Sharadar {table}: duplicate price dates")
    if any(current <= previous for previous, current in zip(dates, dates[1:])):
        raise ValueError(f"Sharadar {table}: price dates are not oldest-first")


def _build_df(rows: list[dict], mapping: dict[str, str]) -> pd.DataFrame:
    """Build a yfinance-compatible DataFrame from Sharadar SF1 rows.

    Input rows are sorted newest-first (from API or cache).
    Output: index = yfinance line item names, columns = date objects (newest first).
    """
    if not rows:
        return pd.DataFrame()

    dates = []
    data = {yf_key: [] for yf_key in mapping}

    for row in rows:
        date_str = row.get("reportperiod")
        if not date_str:
            continue
        dates.append(pd.Timestamp(date_str))
        for yf_key, sharadar_col in mapping.items():
            val = row.get(sharadar_col)
            data[yf_key].append(float(val) if val is not None else None)

    if not dates:
        return pd.DataFrame()

    df = pd.DataFrame(data, index=dates).T
    df.columns = dates
    return df


class SharadarProvider:
    """Fetch data from Sharadar in yfinance-compatible formats.

    Supports SF1 (fundamentals), SEP (equity prices), SFP (fund/ETF prices),
    and TICKERS (metadata).
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str = "./data",
        cache_ttl_hours: int = 168,
    ):
        self.api_key = api_key or os.environ.get("SHARADAR_API_KEY", "")
        self.cache_dir = cache_dir
        self.cache_ttl_hours = cache_ttl_hours
        self._source_observations: dict[str, dict[str, dict]] = {}
        self._capture_state = threading.local()
        self._price_query_locks: dict[str, threading.Lock] = {}
        self._price_query_locks_guard = threading.Lock()
        if not self.api_key:
            raise ValueError(
                "Sharadar API key required. Set SHARADAR_API_KEY environment variable "
                "or pass api_key to SharadarProvider."
            )

    def _record_observation(self, owner_ticker: str, envelope: dict) -> None:
        """Retain exact upstream acquisition evidence for result provenance."""
        identity = envelope["identity"]
        key = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        observation = {
            "source": f"SHARADAR/{identity['table']}",
            "identity": identity,
            "source_fetched_at": envelope["source_fetched_at"],
            "sha256": envelope["sha256"],
        }
        self._publish_observation(owner_ticker, key, observation)

    def _publish_observation(self, owner_ticker: str, key: str, observation: dict) -> None:
        """Publish one raw observation, staging it inside an active transaction."""
        transactions = getattr(self._capture_state, "transactions", [])
        if transactions:
            transactions[-1][(owner_ticker.upper(), key)] = observation.copy()
            return
        self._source_observations.setdefault(owner_ticker.upper(), {})[key] = observation
        for capture in getattr(self._capture_state, "stack", []):
            capture[key] = observation.copy()

    @contextmanager
    def _atomic_provenance(self):
        """Commit a related raw-observation set only when the operation succeeds."""
        transaction: dict[tuple[str, str], dict] = {}
        transactions = getattr(self._capture_state, "transactions", None)
        if transactions is None:
            transactions = []
            self._capture_state.transactions = transactions
        transactions.append(transaction)
        try:
            yield
        except BaseException:
            transactions.pop()
            raise
        else:
            transactions.pop()
            for (owner_ticker, key), observation in transaction.items():
                self._publish_observation(owner_ticker, key, observation)

    @contextmanager
    def capture_provenance(self):
        """Capture exact raw queries consumed by one derived observation."""
        capture: dict[str, dict] = {}
        stack = getattr(self._capture_state, "stack", None)
        if stack is None:
            stack = []
            self._capture_state.stack = stack
        stack.append(capture)
        try:
            yield capture
        finally:
            stack.pop()

    def provenance_for(self, ticker: str) -> list[dict]:
        """Return raw query provenance used while serving one requested ticker."""
        observations = self._source_observations.get(ticker.upper(), {})
        return [observations[key].copy() for key in sorted(observations)]

    def _init_api(self):
        """Lazy-init Nasdaq Data Link with a finite transport timeout and no redirects.

        The client sends the key in the ``x-api-token`` header, and Requests keeps
        that header on a redirect to another host.
        """
        import nasdaqdatalink
        from nasdaqdatalink.connection import Connection

        with _TRANSPORT_LOCK:
            if not getattr(Connection, "_meridian_plugin_transport", False):
                original_execute = Connection.__dict__["execute_request"].__func__

                @classmethod
                def bounded_execute(cls, http_verb, url, **options):
                    options["timeout"] = SHARADAR_HTTP_TIMEOUT
                    options["allow_redirects"] = False
                    _TRANSPORT_STATE.answered = False
                    response = original_execute(cls, http_verb, url, **options)
                    _TRANSPORT_STATE.answered = True
                    return response

                Connection.execute_request = bounded_execute
                Connection._meridian_plugin_transport = True
        nasdaqdatalink.ApiConfig.api_key = self.api_key
        return nasdaqdatalink

    def _fetch_sf1(self, ticker: str, *, strict: bool = False) -> list[dict]:
        """Fetch SF1 annual data for a ticker, using cache.

        With ``strict``, a response that fails a check raises a ``ValueError``
        and a failed query raises ``SourceUnavailable``, instead of returning
        the empty result.
        """
        cache_key = f"{_bare_ticker(ticker)}_sf1"
        identity = _cache_identity(table="SF1", ticker=ticker, dimension="MRY")
        cached = _read_cache(
            self.cache_dir,
            cache_key,
            self.cache_ttl_hours,
            identity=identity,
        )
        if cached:
            rows = cached["payload"].get("rows")
            if isinstance(rows, list):
                try:
                    _validate_sf1_rows(rows, ticker)
                except ValueError:
                    pass
                else:
                    self._record_observation(ticker, cached)
                    return rows

        queried = False
        _TRANSPORT_STATE.answered = False
        try:
            ndl = self._init_api()
            df = ndl.get_table(
                "SHARADAR/SF1",
                ticker=_bare_ticker(ticker),
                dimension="MRY",
                paginate=True,
            )
            queried = True
            if df is None or df.empty:
                logger.warning("Sharadar SF1: no data for %s", ticker)
                return []

            if "reportperiod" not in df.columns:
                raise ValueError("Sharadar SF1: response omitted fiscal report periods")
            # Nasdaq Tables responses are not ordered by contract. Downstream
            # statement columns require the newest fiscal period first.
            df = df.sort_values("reportperiod", ascending=False, kind="stable").reset_index(drop=True)
            rows = _records_from_frame(df)
            _validate_sf1_rows(rows, ticker)
            envelope = _write_cache(
                self.cache_dir,
                cache_key,
                {"rows": rows},
                identity=identity,
            )
            self._record_observation(ticker, envelope)
            return rows

        except Exception as exc:
            logger.error("Sharadar SF1 fetch failed for %s (exception_type=%s)", ticker, type(exc).__name__)
            if strict:
                raise _strict_failure(exc, queried=queried, query="SF1") from exc
            return []

    def get_financials(self, ticker: str) -> pd.DataFrame:
        """Return income statement in yfinance format."""
        return _build_df(self._fetch_sf1(ticker), INCOME_STMT_MAP)

    def get_balance_sheet(self, ticker: str) -> pd.DataFrame:
        """Return balance sheet in yfinance format."""
        return _build_df(self._fetch_sf1(ticker), BALANCE_SHEET_MAP)

    def get_cashflow(self, ticker: str) -> pd.DataFrame:
        """Return cash flow statement in yfinance format."""
        return _build_df(self._fetch_sf1(ticker), CASHFLOW_MAP)

    def _fetch_tickers_meta(
        self, ticker: str, *, require_price_currency: bool = False, strict: bool = False
    ) -> dict | None:
        """Fetch TICKERS metadata, refreshing legacy rows when quote proof is required.

        With ``strict``, a response that fails a check raises a ``ValueError``
        and a failed query raises ``SourceUnavailable``, instead of returning
        ``None``.
        """
        cache_key = f"{_bare_ticker(ticker)}_meta"
        identity = _cache_identity(table="TICKERS", ticker=ticker)
        cached_validation_error = None
        cached = _read_cache(
            self.cache_dir,
            cache_key,
            self.cache_ttl_hours,
            identity=identity,
        )
        if cached:
            meta = cached["payload"].get("meta")
            if isinstance(meta, dict):
                try:
                    _validate_meta(meta, ticker)
                    if require_price_currency:
                        _price_currency_from_metadata(meta)
                except ValueError as exc:
                    if isinstance(exc, _SharadarMetadataConflict):
                        cached_validation_error = exc
                else:
                    self._record_observation(ticker, cached)
                    return meta

        queried = False
        _TRANSPORT_STATE.answered = False
        try:
            ndl = self._init_api()
            df = ndl.get_table(
                "SHARADAR/TICKERS",
                ticker=_bare_ticker(ticker),
                paginate=True,
            )
            queried = True
            if df is None or df.empty:
                if cached_validation_error is not None:
                    raise cached_validation_error
                logger.warning("Sharadar TICKERS: no data for %s", ticker)
                return None
            meta = _select_price_metadata(_records_from_frame(df), ticker)
            if require_price_currency:
                _price_currency_from_metadata(meta)
            envelope = _write_cache(
                self.cache_dir,
                cache_key,
                {"meta": meta},
                identity=identity,
            )
            self._record_observation(ticker, envelope)
            return meta

        except _SharadarMetadataConflict:
            raise
        except Exception as exc:
            logger.error("Sharadar TICKERS fetch failed for %s (exception_type=%s)", ticker, type(exc).__name__)
            if cached_validation_error is not None:
                raise cached_validation_error from exc
            if strict:
                raise _strict_failure(exc, queried=queried, query="TICKERS") from exc
            return None

    def is_etf(self, ticker: str) -> bool:
        """Check if ticker is an ETF/fund (routes to SFP vs SEP)."""
        meta = self._fetch_tickers_meta(ticker)
        if not meta:
            return False
        return _metadata_is_etf(meta)

    def get_price_currency(self, ticker: str) -> str:
        """Return validated price-product currency; never infer USD or use SF1."""
        meta = self._fetch_tickers_meta(ticker, require_price_currency=True)
        if meta is None:
            raise ValueError(f"Sharadar price currency is unavailable for {ticker}")
        return _price_currency_from_metadata(meta)

    def get_ticker_info(self, ticker: str, *, include_beta: bool = False) -> dict:
        """Build yfinance-compatible metadata, deriving beta only when requested."""
        meta = self._fetch_tickers_meta(ticker) or {}
        is_fund = _metadata_is_etf(meta)
        rows = [] if is_fund else self._fetch_sf1(ticker)
        latest = rows[0] if rows else {}

        category = (meta.get("category") or "").lower()
        quote_type = "ETF" if is_fund else _CATEGORY_TO_QUOTE_TYPE.get(category, "EQUITY")

        provider_name = validated_security_name(meta.get("name"), _bare_ticker(ticker))
        info = {
            "shortName": provider_name,
            "longName": provider_name,
            "sector": meta.get("sector"),
            "industry": meta.get("sicindustry"),
            "quoteType": quote_type,
            "exchange": meta.get("exchange"),
            "currency": meta.get("currency"),
            "financialCurrency": None if is_fund else meta.get("currency"),
        }

        shares = latest.get("sharesbas")
        if shares is not None:
            info["sharesOutstanding"] = int(float(shares))

        price = latest.get("price")
        if price is not None and shares is not None:
            info["marketCap"] = float(price) * float(shares)

        # Valuation requires a provider-pure beta. Derive it only for equities
        # from official Sharadar total-return closes versus SPY.
        if include_beta and quote_type != "ETF" and meta:
            try:
                info["beta"] = self.get_beta(ticker)
            except ValueError as exc:
                logger.warning(
                    "Sharadar beta unavailable for %s (exception_type=%s)",
                    ticker,
                    type(exc).__name__,
                )

        return {k: v for k, v in info.items() if v is not None}

    def validate_ticker(self, ticker: str, *, strict: bool = False) -> dict:
        """Validate ticker via TICKERS table."""
        meta = self._fetch_tickers_meta(ticker, strict=strict)
        if not meta:
            return {"valid": False, "name": None}
        provider_name = validated_security_name(meta.get("name"), _bare_ticker(ticker))
        if meta.get("isdelisted") == "Y":
            return {"valid": False, "name": provider_name}
        return {"valid": True, "name": provider_name}

    def _fetch_prices(
        self,
        ticker: str,
        table: str = "SEP",
        start: str = "1990-01-01",
        *,
        force_refresh: bool = False,
        strict: bool = False,
    ) -> pd.DataFrame:
        """Fetch daily prices from SEP or SFP table.

        Returns DataFrame with columns: date, open, high, low, close, volume,
        and closeadj if available. ``force_refresh`` bypasses the
        provider price cache when the engine has already rejected its published
        CSV generation and needs an authoritative replacement. With ``strict``,
        a failed query raises ``SourceUnavailable`` instead of returning an
        empty frame.
        """
        table = table.upper()
        if table not in {"SEP", "SFP"}:
            raise ValueError(f"Unsupported Sharadar price table: {table!r}")

        # History start defines the query result and therefore belongs in the
        # cache identity. Reusing a recent-history cache for an older request
        # silently truncates every downstream return series.
        cache_key, normalized_start = _prices_cache_key(ticker, table, start)
        identity = _cache_identity(
            table=table,
            ticker=ticker,
            start=normalized_start,
        )
        # Many tickers read the same SPY beta window. One download serves all
        # concurrent readers of one query.
        with self._price_query_lock(cache_key):
            cached = (
                None
                if force_refresh
                else _read_cache(
                    self.cache_dir,
                    cache_key,
                    _PRICE_CACHE_TTL_HOURS,
                    identity=identity,
                )
            )
            if cached:
                rows = cached["payload"].get("rows")
                if isinstance(rows, list):
                    _validate_price_rows(rows, ticker, normalized_start, table)
                    self._record_observation(ticker, cached)
                    df = pd.DataFrame(rows)
                    if not df.empty:
                        df["date"] = pd.to_datetime(df["date"])
                    return df

            queried = False
            _TRANSPORT_STATE.answered = False
            try:
                ndl = self._init_api()
                df = ndl.get_table(
                    f"SHARADAR/{table}",
                    ticker=_bare_ticker(ticker),
                    date={"gte": normalized_start},
                    paginate=True,
                )
                queried = True
                if df is None or df.empty:
                    logger.warning("Sharadar %s: no price data for %s", table, ticker)
                    return pd.DataFrame()

                df["date"] = pd.to_datetime(df["date"], errors="raise")
                # Nasdaq Tables responses are not ordered by contract. Every price
                # consumer expects a strictly increasing market-date sequence.
                df = df.sort_values("date", ascending=True, kind="stable").reset_index(drop=True)

                rows = _records_from_frame(df)
                _validate_price_rows(rows, ticker, normalized_start, table)
                with _price_window_family_lock(self.cache_dir, cache_key):
                    envelope = _write_cache(
                        self.cache_dir,
                        cache_key,
                        {"rows": rows},
                        identity=identity,
                    )
                self._record_observation(ticker, envelope)

            except ValueError as exc:
                if strict:
                    raise _strict_failure(exc, queried=queried, query=table) from exc
                raise
            except Exception as exc:
                logger.error(
                    "Sharadar %s fetch failed for %s (exception_type=%s)",
                    table,
                    ticker,
                    type(exc).__name__,
                )
                if strict:
                    raise _strict_failure(exc, queried=queried, query=table) from exc
                return pd.DataFrame()
            try:
                _prune_expired_price_windows(self.cache_dir, cache_key)
            except OSError as exc:
                # The window is already written. A failed cleanup must not fail the read.
                logger.warning(
                    "Sharadar price-window cleanup failed for %s (exception_type=%s)",
                    ticker,
                    type(exc).__name__,
                )
            return df

    def _price_query_lock(self, cache_key: str) -> threading.Lock:
        """Return the lock that serializes reads and writes of one price query."""
        with self._price_query_locks_guard:
            return self._price_query_locks.setdefault(cache_key, threading.Lock())

    @staticmethod
    def _monthly_adjusted_close(prices: pd.DataFrame, *, label: str) -> pd.Series:
        """Return unique, positive month-end official adjusted closes."""
        required = {"date", "closeadj"}
        if prices is None or prices.empty or not required.issubset(prices.columns):
            raise ValueError(f"Sharadar beta: {label} adjusted-close history is unavailable")
        dates = pd.to_datetime(prices["date"], errors="raise")
        if dates.hasnans:
            raise ValueError(f"Sharadar beta: {label} price dates are missing")
        if dates.duplicated().any():
            raise ValueError(f"Sharadar beta: {label} price dates are duplicated")
        values = pd.to_numeric(prices["closeadj"], errors="coerce")
        numeric = values.to_numpy(dtype=float)
        if not np.isfinite(numeric).all() or (numeric <= 0).any():
            raise ValueError(f"Sharadar beta: {label} adjusted closes are invalid")
        series = pd.Series(numeric, index=dates)
        monthly = series.groupby(series.index.to_period("M")).last()
        current_month = pd.Timestamp.now(tz="UTC").tz_localize(None).to_period("M")
        return monthly.loc[monthly.index < current_month].tail(_BETA_LOOKBACK_YEARS * 12 + 1)

    def get_beta(self, ticker: str) -> float:
        """Derive five-year monthly equity beta from Sharadar total returns.

        Beta is sample covariance(target returns, SPY returns) divided by the
        sample variance of SPY returns. The aligned observation months must be
        unique and consecutive, with at least 24 monthly return pairs.
        """
        with self._atomic_provenance():
            start = (pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=_BETA_LOOKBACK_YEARS, months=2)).date()
            target = self._fetch_prices(ticker, "SEP", start=start.isoformat())
            benchmark = self._fetch_prices(_BETA_BENCHMARK, "SFP", start=start.isoformat())

            target_monthly = self._monthly_adjusted_close(target, label=ticker)
            benchmark_monthly = self._monthly_adjusted_close(benchmark, label=_BETA_BENCHMARK)
            common = target_monthly.index.intersection(benchmark_monthly.index).sort_values()
            minimum_prices = _BETA_MIN_RETURNS + 1
            if len(common) < minimum_prices:
                raise ValueError(f"Sharadar beta: fewer than {_BETA_MIN_RETURNS} aligned monthly returns")
            ordinals = common.astype("int64").to_numpy()
            if not np.all(np.diff(ordinals) == 1):
                raise ValueError("Sharadar beta: aligned monthly history has gaps")
            target_returns = target_monthly.loc[common].pct_change().dropna().to_numpy(dtype=float)
            benchmark_returns = benchmark_monthly.loc[common].pct_change().dropna().to_numpy(dtype=float)
            if len(target_returns) < _BETA_MIN_RETURNS:
                raise ValueError("Sharadar beta: insufficient return observations")
            benchmark_variance = float(np.var(benchmark_returns, ddof=1))
            if not np.isfinite(benchmark_variance) or benchmark_variance <= 0:
                raise ValueError("Sharadar beta: benchmark variance is unavailable")
            covariance = float(np.cov(target_returns, benchmark_returns, ddof=1)[0, 1])
            beta = covariance / benchmark_variance
            if not np.isfinite(beta) or beta < -5 or beta > 10:
                raise ValueError("Sharadar beta is outside the supported range")
            return float(beta)

    def _compute_dividend_adjusted_close(self, prices_df: pd.DataFrame, ticker: str) -> pd.Series:
        """Reject non-identifiable adjusted-close reconstruction.

        SF1 exposes annual aggregate cash dividends and filing/fiscal dates,
        not the per-share ex-dividend events needed to reconstruct a daily
        total-return series. Treating those dates as ex-dates fabricates market
        history, so Strategy requires Sharadar's official ``closeadj`` field.
        """
        raise ValueError(
            f"Sharadar adjusted close is unavailable for {ticker}; "
            "annual SF1 dividends cannot reconstruct exact ex-dividend history"
        )

    def price_frame(
        self,
        ticker: str,
        table: str,
        start: str = "1990-01-01",
        *,
        force_refresh: bool = False,
        strict: bool = False,
    ) -> pd.DataFrame:
        """Return daily prices of SEP (equities) or SFP (ETFs/funds) in the column order of the in-tree price CSV.

        The caller selects ``table`` from the TICKERS row, as
        ``download_prices_csv`` did in Meridian. The index is ``Date`` and the
        columns are Open, High, Low, Volume, Close and Adj Close. A query with no
        rows gives a frame with these columns and no rows.
        """
        df = self._fetch_prices(ticker, table, start, force_refresh=force_refresh, strict=strict)

        if df.empty:
            empty = pd.DataFrame(columns=["Open", "High", "Low", "Volume", "Close", "Adj Close"])
            empty.index = pd.DatetimeIndex([], name="Date")
            return empty

        out = pd.DataFrame()
        out["Date"] = pd.to_datetime(df["date"])

        for src, dst in [("open", "Open"), ("high", "High"), ("low", "Low"), ("volume", "Volume")]:
            if src in df.columns:
                out[dst] = df[src].values
            else:
                out[dst] = np.nan

        if "close" not in df.columns:
            raise ValueError(f"Sharadar {table}: no split-adjusted price-only close data for {ticker}")
        close = pd.to_numeric(df["close"], errors="coerce")
        if not np.isfinite(close.to_numpy(dtype=float)).all():
            raise ValueError(f"Sharadar {table}: incomplete price-only close data for {ticker}")
        if (close <= 0).any():
            raise ValueError(f"Sharadar {table}: non-positive price-only close data for {ticker}")
        out["Close"] = close.to_numpy(dtype=float)

        if "closeadj" not in df.columns:
            self._compute_dividend_adjusted_close(df, ticker)
        adjusted = pd.to_numeric(df["closeadj"], errors="coerce")
        if not np.isfinite(adjusted.to_numpy(dtype=float)).all():
            raise ValueError(f"Sharadar {table}: incomplete adjusted close data for {ticker}")
        if (adjusted <= 0).any():
            raise ValueError(f"Sharadar {table}: non-positive adjusted close data for {ticker}")
        out["Adj Close"] = adjusted.to_numpy(dtype=float)

        out = out.set_index("Date")
        out.index.name = "Date"
        return out

    def get_dividends(self, ticker: str, *, strict: bool = False) -> pd.Series:
        """Get complete annual dividend-per-share observations from SF1.

        Computes DPS = abs(ncfdiv) / shareswa for each complete annual period
        and preserves an explicit NaN for a period with missing/invalid inputs.
        The index is the SF1 fiscal ``reportperiod``. These values are annual
        aggregates, not ex-dividend events; callers must use the
        ``annual_fiscal`` observation kind exposed by ``TickerData``.
        """
        rows = self._fetch_sf1(ticker, strict=strict)
        if not rows:
            return pd.Series(dtype=float)

        dates = []
        divs = []
        for row in rows:
            ncfdiv = row.get("ncfdiv")
            shareswa = row.get("shareswa")
            date_str = row.get("reportperiod")
            if not date_str:
                raise ValueError(f"Sharadar SF1: dividend row has no fiscal report period for {ticker}")
            if ncfdiv is None or shareswa is None:
                dps = np.nan
            else:
                shareswa = float(shareswa)
                ncfdiv = float(ncfdiv)
                if not np.isfinite(shareswa) or not np.isfinite(ncfdiv) or shareswa <= 0:
                    dps = np.nan
                else:
                    dps = abs(ncfdiv) / shareswa
            dates.append(pd.Timestamp(date_str))
            divs.append(dps)

        if not dates:
            return pd.Series(dtype=float)

        s = pd.Series(divs, index=dates, name="Dividends")
        return s.iloc[::-1]
