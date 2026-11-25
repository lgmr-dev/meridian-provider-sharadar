"""Wire encoding of the Meridian market-data contract (`contracts/plugin-runtime/v1`)."""

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pandas as pd

DECIMAL = re.compile(r"-?\d+(\.\d+)?", re.ASCII)
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", re.ASCII)


class EncodingFault(ValueError):
    """A value has no encoding in the contract."""


def decimal_or_null(value: object) -> str | None:
    """Return the contract decimal of a number, or ``None`` for a missing value.

    A binary float becomes the plain expansion of its shortest round-trip text,
    ``format(Decimal(repr(x)), "f")``, so the string parses to the same double.
    """
    if value is None:
        return None
    if isinstance(value, bool | np.bool_):
        raise EncodingFault("a boolean is not a decimal")
    if isinstance(value, int | np.integer):
        return str(int(value))
    if isinstance(value, float | np.floating):
        number = float(value)
        if math.isnan(number):
            return None
        if not math.isfinite(number):
            raise EncodingFault("a decimal is not finite")
        text = format(Decimal(repr(number)), "f")
        if DECIMAL.fullmatch(text) is None:
            raise EncodingFault("a decimal has no plain form")
        return text
    raise EncodingFault(f"{type(value).__name__} is not a decimal")


def integer_or_null(value: object) -> int | None:
    """Return a JSON integer, or ``None`` for a missing value."""
    if value is None:
        return None
    if isinstance(value, bool | np.bool_) or not isinstance(value, int | np.integer):
        raise EncodingFault(f"{type(value).__name__} is not an integer")
    return int(value)


def string_or_null(value: object) -> str | None:
    """Return a JSON string, or ``None`` for a missing value."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise EncodingFault(f"{type(value).__name__} is not a string")
    return value


def calendar_date(value: object) -> str:
    """Return ``YYYY-MM-DD`` for a date with no time of day."""
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise EncodingFault("a date is missing")
    if stamp.tzinfo is not None or stamp != stamp.normalize():
        raise EncodingFault("a date has a time of day or a time zone")
    return stamp.date().isoformat()


def utc_timestamp(text: str) -> str:
    """Return an RFC 3339 UTC timestamp with ``Z`` from ``datetime.isoformat()`` text with ``+00:00``."""
    if not isinstance(text, str) or not text.endswith("+00:00"):
        raise EncodingFault("a timestamp is not UTC with +00:00")
    encoded = text.removesuffix("+00:00") + "Z"
    if TIMESTAMP.fullmatch(encoded) is None:
        raise EncodingFault("a timestamp has no RFC 3339 form")
    return encoded


def now_timestamp() -> str:
    """Return the current time as an RFC 3339 UTC timestamp with ``Z``."""
    return utc_timestamp(datetime.now(UTC).isoformat())


def canonical_json(value: object) -> str:
    """Return the canonical JSON text: sorted keys, no spaces, ASCII escapes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 of the canonical JSON text."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def column(name: str, kind: str, unit: str | None, nullable: bool) -> dict:
    """Return one column of a contract table."""
    return {"name": name, "type": kind, "unit": unit, "nullable": nullable}
