"""Shared Stock Selection boundary validators."""

from __future__ import annotations

import calendar
import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path


def _load_stock_selection_identity() -> dict[str, int | str]:
    """Load the packaged cross-runtime screening identity or fail closed."""
    try:
        value = json.loads(Path(__file__).with_name("stock_selection_identity.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Stock Selection screening identity is unavailable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"screening_algorithm_version", "screening_cache_schema_version"}
        or not isinstance(value["screening_cache_schema_version"], int)
        or isinstance(value["screening_cache_schema_version"], bool)
        or value["screening_cache_schema_version"] < 1
        or not isinstance(value["screening_algorithm_version"], str)
        or re.fullmatch(
            r"stock-selection-[0-9]{4}-[0-9]{2}-[0-9]{2}-r[1-9][0-9]*", value["screening_algorithm_version"]
        )
        is None
    ):
        raise RuntimeError("Stock Selection screening identity is malformed")
    return value


_STOCK_SELECTION_IDENTITY = _load_stock_selection_identity()
SCREENING_CACHE_SCHEMA_VERSION = int(_STOCK_SELECTION_IDENTITY["screening_cache_schema_version"])
SCREENING_ALGORITHM_VERSION = str(_STOCK_SELECTION_IDENTITY["screening_algorithm_version"])

MAX_SECURITY_NAME_CHARACTERS = 200
MAX_DISPLAY_TEXT_CHARACTERS = 200
MAX_STOCK_SELECTION_TICKER_CHARACTERS = 20
SHARADAR_SOURCE_PROVENANCE_SCHEMA_VERSION = 2

_STOCK_SELECTION_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^]{0,19}$")
_B3_FII_OR_ETF_CANDIDATE_RE = re.compile(r"^[A-Z]{4}11[BF]?\.SA$")
_SUPPORTED_US_CLASS_SHARE_RE = re.compile(r"^(?P<issuer>[A-Z0-9]{1,5})\.(?P<share_class>[AB])$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class StockSelectionAbandoned(RuntimeError):
    """Internal cooperative-cancellation signal for abandoned Stock Selection work."""


def _decimal_integer_ratio(value: int | float) -> tuple[int, int]:
    """Return the exact ratio encoded by one finite JSON number's decimal form."""
    if isinstance(value, int):
        return value, 1
    mantissa, separator, exponent_text = str(value).lower().partition("e")
    exponent = int(exponent_text) if separator else 0
    whole, point, fraction = mantissa.partition(".")
    digits = whole + (fraction if point else "")
    coefficient = int(digits)
    power = exponent - len(fraction)
    return (coefficient * 10**power, 1) if power >= 0 else (coefficient, 10 ** (-power))


def published_finite_value(value: object, decimal_places: int) -> float | None:
    """Return one finite JSON number with exact decimal half-up publication."""
    if not isinstance(decimal_places, int) or isinstance(decimal_places, bool) or not 0 <= decimal_places <= 12:
        raise ValueError("Published value decimal places are invalid")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        return None
    value_integer, value_scale = _decimal_integer_ratio(value)
    sign = -1 if value_integer < 0 else 1
    scale = 10**decimal_places
    quotient, remainder = divmod(abs(value_integer) * scale, value_scale)
    if remainder * 2 >= value_scale:
        quotient += 1
    try:
        published = sign * quotient / scale
    except OverflowError:
        return None
    return published if math.isfinite(published) else None


def published_valuation_cost_of_equity(
    beta: object,
    ticker: object,
    us_risk_free_rate: object,
    us_erp: object,
    br_country_risk: object,
) -> float | None:
    """Publish the row discount rate from its exact cohort market inputs."""
    inputs = (beta, us_risk_free_rate, us_erp, br_country_risk)
    if not isinstance(ticker, str) or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        for value in inputs
    ):
        return None
    raw = float(us_risk_free_rate) + float(beta) * float(us_erp)
    if ticker.endswith(".SA"):
        raw += float(br_country_risk)
    return published_finite_value(raw, 4)


def published_positive_value(value: object, decimal_places: int) -> float | None:
    """Return a finite positive value using exact decimal half-up publication."""
    published = published_finite_value(value, decimal_places)
    return published if published is not None and published > 0 else None


def published_positive_average(values: object, decimal_places: int) -> float | None:
    """Average already-published positive values without binary-float summation."""
    if not isinstance(decimal_places, int) or isinstance(decimal_places, bool) or not 0 <= decimal_places <= 12:
        raise ValueError("Published value decimal places are invalid")
    if not isinstance(values, (list, tuple)) or not values:
        return None
    scale = 10**decimal_places
    scaled_values: list[int] = []
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            return None
        value_integer, value_scale = _decimal_integer_ratio(value)
        scaled, remainder = divmod(value_integer * scale, value_scale)
        if remainder != 0 or scaled <= 0:
            return None
        scaled_values.append(scaled)
    quotient, remainder = divmod(sum(scaled_values), len(scaled_values))
    if remainder * 2 >= len(scaled_values):
        quotient += 1
    published = quotient / scale
    return published if math.isfinite(published) and published > 0 else None


def published_positive_ratio(numerator: object, denominator: object, decimal_places: int) -> float | None:
    """Return a finite positive ratio using exact decimal half-up publication.

    The source values are finite JSON numbers. Their shortest decimal forms are
    converted to integer ratios before division so an IEEE-754 approximation
    cannot move an exact decimal half-tie to the neighboring published value.
    JavaScript mirrors this integer arithmetic with ``BigInt``.
    """
    if not isinstance(decimal_places, int) or isinstance(decimal_places, bool) or not 0 <= decimal_places <= 12:
        raise ValueError("Published ratio decimal places are invalid")
    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or not isinstance(numerator, (int, float))
        or not isinstance(denominator, (int, float))
        or numerator <= 0
        or denominator <= 0
        or (isinstance(numerator, float) and not math.isfinite(numerator))
        or (isinstance(denominator, float) and not math.isfinite(denominator))
    ):
        return None

    numerator_value, numerator_scale = _decimal_integer_ratio(numerator)
    denominator_value, denominator_scale = _decimal_integer_ratio(denominator)
    scale = 10**decimal_places
    scaled_numerator = numerator_value * denominator_scale * scale
    scaled_denominator = numerator_scale * denominator_value
    quotient, remainder = divmod(scaled_numerator, scaled_denominator)
    if remainder * 2 >= scaled_denominator:
        quotient += 1
    try:
        published = quotient / scale
    except OverflowError:
        return None
    return published if math.isfinite(published) and published > 0 else None


def validated_stock_selection_ticker_identity(value: object, *, data_source: str | None = None) -> str:
    """Return one unambiguous Stock Selection/provider ticker identity.

    ``.SA`` is Meridian's exchange suffix, not a removable substring. Embedded
    or repeated occurrences are therefore invalid. Sharadar is provider-pure
    for US assets; its only supported ``.SA`` request shape is the documented
    B3 11/11B/11F classification candidate, which may resolve to a selected-
    provider ETF or the explicit yfinance FII exception.
    """
    if not isinstance(value, str):
        raise ValueError("Stock Selection ticker must be a string")
    ticker = value.strip().upper()
    if not _STOCK_SELECTION_TICKER_RE.fullmatch(ticker):
        raise ValueError("Stock Selection ticker syntax is invalid")
    if ".SA" in ticker and (not ticker.endswith(".SA") or ticker.count(".SA") != 1):
        raise ValueError("Stock Selection .SA suffix must occur exactly once at the end")
    if data_source == "sharadar" and ticker.endswith(".SA") and not _B3_FII_OR_ETF_CANDIDATE_RE.fullmatch(ticker):
        raise ValueError("Sharadar does not support this Brazilian ticker identity")
    return ticker


def sharadar_provider_ticker(value: object) -> str:
    """Return the exact provider symbol for one supported Sharadar request."""
    return validated_stock_selection_ticker_identity(value, data_source="sharadar").removesuffix(".SA")


def yfinance_provider_ticker(value: object) -> str:
    """Return the yfinance symbol for one canonical Stock Selection identity.

    Meridian's supported US ``.A``/``.B`` share-class forms stay dotted in
    requests, results, and universe evidence, while Yahoo uses a hyphen. Other
    dotted terminal components are Yahoo exchange suffixes and must remain
    exact provider identity (for example ``7203.T`` and ``VOD.L``).
    """
    ticker = validated_stock_selection_ticker_identity(value, data_source="yfinance")
    class_share = _SUPPORTED_US_CLASS_SHARE_RE.fullmatch(ticker)
    if class_share is None:
        return ticker
    return f"{class_share.group('issuer')}-{class_share.group('share_class')}"


def _shift_calendar_date(value: date, *, years: int = 0, months: int = 0, days: int = 0) -> date:
    """Shift a date with pandas-compatible end-of-month clipping."""
    month_index = value.year * 12 + value.month - 1 + years * 12 + months
    shifted_year, shifted_month_index = divmod(month_index, 12)
    shifted_month = shifted_month_index + 1
    shifted_day = min(value.day, calendar.monthrange(shifted_year, shifted_month)[1])
    return date(shifted_year, shifted_month, shifted_day) + timedelta(days=days)


def _sharadar_history_expected_start(
    observation_key: str,
    request_date: date,
    *,
    analysis_periods: int | None = None,
) -> date | None:
    """Recompute one supported query start from its recorded request date."""
    if not observation_key.startswith("history:"):
        return None
    try:
        query = json.loads(observation_key.removeprefix("history:"))
    except (TypeError, ValueError):
        return None
    if not isinstance(query, dict) or query.get("auto_adjust") is not False or query.get("timeout") != 10:
        return None
    if set(query) == {"auto_adjust", "period", "timeout"} and query.get("period") == "5d":
        return request_date - timedelta(days=15)
    if set(query) == {"auto_adjust", "period", "timeout"} and query.get("period") == "6y":
        return _shift_calendar_date(request_date, years=-6, days=-7)
    if (
        set(query) == {"auto_adjust", "interval", "period", "timeout"}
        and isinstance(analysis_periods, int)
        and not isinstance(analysis_periods, bool)
        and 2 <= analysis_periods <= 20
        and query.get("period") == f"{analysis_periods}y"
        and query.get("interval") == "1mo"
    ):
        return _shift_calendar_date(request_date, years=-analysis_periods, days=-7)
    return None


def _query_start_matches(
    observation_key: str,
    start: date,
    *timestamps: datetime,
    analysis_periods: int | None = None,
) -> bool:
    """Bind a query start to each request timestamp without reversing clipped shifts."""
    expected = [
        _sharadar_history_expected_start(
            observation_key,
            timestamp.astimezone(UTC).date(),
            analysis_periods=analysis_periods,
        )
        for timestamp in timestamps
    ]
    return all(value is not None and abs((start - value).days) <= 1 for value in expected)


def _beta_start_matches(start: date, *timestamps: datetime) -> bool:
    """Bind the five-year-two-month beta start to request timestamps."""
    return all(
        abs((start - _shift_calendar_date(timestamp.astimezone(UTC).date(), years=-5, months=-2)).days) <= 1
        for timestamp in timestamps
    )


def valid_sharadar_source_observations(
    value: object,
    *,
    schema_version: object,
    observations: object,
    ticker: str,
    asset_type: str,
    requires_sf1: bool,
    requires_beta: bool,
    analysis_periods: int | None = None,
) -> bool:
    """Validate exact raw Sharadar query identities and observation bindings.

    Result provenance is a reproducibility boundary, not merely a provider
    label. Schema version 2 binds every derived provider observation to the
    complete raw query identities consumed while producing it, then requires
    exact equality between the bound and published raw envelopes. Coverage is
    type/model-aware: every row needs metadata plus its current-price table,
    statement-derived rows need annual SF1, and intrinsic valuations need both
    sides of the target-versus-SPY beta calculation.
    """

    if (
        schema_version != SHARADAR_SOURCE_PROVENANCE_SCHEMA_VERSION
        or not isinstance(value, list)
        or not value
        or not isinstance(observations, dict)
        or not observations
        or asset_type not in {"stock", "financial", "reit", "fii", "etf"}
        or not isinstance(requires_sf1, bool)
        or not isinstance(requires_beta, bool)
        or (asset_type == "etf" and requires_beta)
    ):
        return False
    try:
        requested_ticker = sharadar_provider_ticker(ticker)
    except ValueError:
        return False

    now = datetime.now(UTC)
    price_table = "SFP" if asset_type == "etf" else "SEP"
    required = {
        ("TICKERS", requested_ticker),
        (price_table, requested_ticker),
    }
    if requires_sf1:
        required.add(("SF1", requested_ticker))
    if requires_beta:
        required.update({("SEP", requested_ticker), ("SFP", "SPY")})

    observed: set[tuple[str, str]] = set()
    raw_by_identity: dict[str, dict] = {}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "source",
            "identity",
            "source_fetched_at",
            "sha256",
        }:
            return False
        identity = entry.get("identity")
        if not isinstance(identity, dict):
            return False
        table = identity.get("table")
        identity_ticker = identity.get("ticker")
        if table == "TICKERS":
            expected_keys = {"provider", "table", "ticker"}
        elif table == "SF1":
            expected_keys = {"provider", "table", "ticker", "dimension"}
        elif table in {"SEP", "SFP"}:
            expected_keys = {"provider", "table", "ticker", "start"}
        else:
            return False
        if (
            set(identity) != expected_keys
            or identity.get("provider") != "sharadar"
            or not isinstance(identity_ticker, str)
            or identity_ticker != identity_ticker.upper()
            or entry.get("source") != f"SHARADAR/{table}"
            or not isinstance(entry.get("sha256"), str)
            or _SHA256_RE.fullmatch(entry["sha256"]) is None
        ):
            return False
        if table == "SF1" and identity.get("dimension") != "MRY":
            return False
        if table in {"SEP", "SFP"}:
            start = identity.get("start")
            try:
                parsed_start = date.fromisoformat(start) if isinstance(start, str) else None
            except ValueError:
                return False
            if parsed_start is None or parsed_start.isoformat() != start or parsed_start > now.date():
                return False
        try:
            fetched = datetime.fromisoformat(str(entry.get("source_fetched_at", "")).replace("Z", "+00:00"))
        except ValueError:
            return False
        if fetched.tzinfo is None or fetched.astimezone(UTC) > now:
            return False

        relation = (table, identity_ticker)
        allowed = {
            ("TICKERS", requested_ticker),
            ("SF1", requested_ticker),
            (price_table, requested_ticker),
            ("SFP", "SPY"),
        }
        if relation not in allowed:
            return False
        canonical_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        if canonical_identity in raw_by_identity:
            return False
        raw_by_identity[canonical_identity] = {
            "identity": identity,
            "relation": relation,
            "source_fetched_at": fetched,
        }
        observed.add(relation)

    statement_keys = {"financials", "balance_sheet", "cashflow", "dividends"}
    bound_identities: set[str] = set()
    for observation_key, observation in observations.items():
        if not isinstance(observation_key, str) or not isinstance(observation, dict):
            return False
        source_identities = observation.get("source_identities")
        if not isinstance(source_identities, list):
            return False
        unavailable = observation.get("status") == "unavailable"
        if not unavailable and not source_identities:
            return False
        if observation_key in statement_keys:
            if asset_type == "etf":
                return False
            allowed_for_observation = {("SF1", requested_ticker)}
            required_for_observation = allowed_for_observation
        elif observation_key == "info":
            allowed_for_observation = (
                {("TICKERS", requested_ticker)}
                if asset_type == "etf"
                else {("TICKERS", requested_ticker), ("SF1", requested_ticker)}
                | ({("SEP", requested_ticker), ("SFP", "SPY")} if requires_beta else set())
            )
            required_for_observation = {("TICKERS", requested_ticker)}
        elif observation_key == "history" or observation_key.startswith("history:"):
            allowed_for_observation = {
                ("TICKERS", requested_ticker),
                (price_table, requested_ticker),
            }
            required_for_observation = allowed_for_observation
        else:
            return False

        observation_sources: list[dict] = []
        observation_relations: set[tuple[str, str]] = set()
        observation_identities: set[str] = set()
        for identity in source_identities:
            if not isinstance(identity, dict):
                return False
            canonical_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            source = raw_by_identity.get(canonical_identity)
            relation = source["relation"] if source is not None else None
            if (
                relation is None
                or relation not in allowed_for_observation
                or canonical_identity in observation_identities
            ):
                return False
            observation_identities.add(canonical_identity)
            observation_relations.add(relation)
            observation_sources.append(source)
            bound_identities.add(canonical_identity)
        if not unavailable and not required_for_observation.issubset(observation_relations):
            return False
        if unavailable:
            continue

        if observation_key in statement_keys:
            if len(observation_sources) != 1:
                return False
        elif observation_key == "info":
            metadata_sources = [
                source for source in observation_sources if source["relation"] == ("TICKERS", requested_ticker)
            ]
            target_beta_sources = [
                source for source in observation_sources if source["relation"] == ("SEP", requested_ticker)
            ]
            benchmark_sources = [source for source in observation_sources if source["relation"] == ("SFP", "SPY")]
            if len(metadata_sources) != 1:
                return False
            if asset_type == "etf":
                if len(observation_sources) != 1:
                    return False
            elif requires_beta:
                if len(target_beta_sources) != 1 or len(benchmark_sources) != 1:
                    return False
                target_start = date.fromisoformat(target_beta_sources[0]["identity"]["start"])
                benchmark_start = date.fromisoformat(benchmark_sources[0]["identity"]["start"])
                if target_start != benchmark_start:
                    return False
                if not _beta_start_matches(
                    target_start,
                    target_beta_sources[0]["source_fetched_at"],
                    benchmark_sources[0]["source_fetched_at"],
                ):
                    return False
        else:
            price_sources = [
                source for source in observation_sources if source["relation"] == (price_table, requested_ticker)
            ]
            metadata_sources = [
                source for source in observation_sources if source["relation"] == ("TICKERS", requested_ticker)
            ]
            if len(observation_sources) != 2 or len(metadata_sources) != 1 or len(price_sources) != 1:
                return False
            price_source = price_sources[0]
            start = date.fromisoformat(price_source["identity"]["start"])
            try:
                retrieved_at = datetime.fromisoformat(str(observation.get("retrieved_at", "")).replace("Z", "+00:00"))
            except ValueError:
                return False
            if retrieved_at.tzinfo is None or not _query_start_matches(
                observation_key,
                start,
                price_source["source_fetched_at"],
                retrieved_at,
                analysis_periods=analysis_periods,
            ):
                return False

    return required.issubset(observed) and bound_identities == set(raw_by_identity)


def is_valid_security_name(value: object, *, allow_empty: bool = True) -> bool:
    """Return whether a provider/display name is a bounded printable string."""
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return len(value) <= MAX_SECURITY_NAME_CHARACTERS and value.isprintable()


def validated_security_name(value: object, fallback: str) -> str:
    """Normalize one external provider name or fail on malformed present data."""
    if value is None or value == "":
        return fallback
    if not isinstance(value, str):
        raise ValueError("Provider security name is not a string")
    if not is_valid_security_name(value, allow_empty=False):
        raise ValueError("Provider security name is not a bounded printable string")
    normalized = value.strip()
    if not normalized:
        return fallback
    return normalized


def security_name_from_info(info: dict, ticker: str) -> str:
    """Select the first present provider name while rejecting malformed values."""
    for field in ("longName", "shortName"):
        value = info.get(field)
        if value is not None and value != "":
            return validated_security_name(value, ticker)
    return ticker


def is_valid_optional_display_text(value: object) -> bool:
    """Return whether optional provider display metadata is safe to render."""
    return value is None or (
        isinstance(value, str) and len(value) <= MAX_DISPLAY_TEXT_CHARACTERS and value.isprintable()
    )


def validated_optional_display_text(value: object, label: str) -> str | None:
    """Normalize optional provider display metadata or fail at ingestion."""
    if value is None or value == "":
        return None
    if not is_valid_optional_display_text(value):
        raise ValueError(f"Provider {label} is not a bounded printable string")
    normalized = value.strip()
    return normalized or None
