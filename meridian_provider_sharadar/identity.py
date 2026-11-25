"""Ticker and security-name validators of Meridian `server/stock_selection_contract.py` at `aa5d0cb`."""

import re

MAX_SECURITY_NAME_CHARACTERS = 200

_STOCK_SELECTION_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^]{0,19}$")
_B3_FII_OR_ETF_CANDIDATE_RE = re.compile(r"^[A-Z]{4}11[BF]?\.SA$")


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
