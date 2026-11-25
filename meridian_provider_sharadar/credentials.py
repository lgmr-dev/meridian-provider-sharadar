"""Check of a candidate Sharadar API key, as Meridian `server/platform/sharadar_key.py` did at `aa5d0cb`."""

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

TICKERS_URL = "https://data.nasdaq.com/api/v3/datatables/SHARADAR/TICKERS.json"
TIMEOUT_SECONDS = 15


class CredentialCheckUnavailable(Exception):
    """The key check got no usable answer from Nasdaq Data Link."""


@dataclass(frozen=True)
class CredentialResult:
    valid: bool
    code: str | None


def check_candidate(candidate: str) -> CredentialResult:
    """Query one TICKERS row with the candidate key in the ``X-Api-Token`` header.

    The key never goes into the URL, a log line or an exception text. The plug-in
    rejects a candidate that is not printable ASCII with no request. A Nasdaq
    Data Link key is printable ASCII, and the in-tree check sent such a
    candidate in the URL, where Nasdaq Data Link rejected it.
    """
    if not candidate.isascii() or not candidate.isprintable():
        return CredentialResult(valid=False, code="key_rejected")
    try:
        response = requests.get(
            TICKERS_URL,
            params={"ticker": "AAPL"},
            headers={"X-Api-Token": candidate, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        logger.warning("Sharadar key check failed (exception_type=%s)", type(exc).__name__)
        raise CredentialCheckUnavailable("the key check failed") from None
    if response.status_code == 400:
        return CredentialResult(valid=False, code="key_rejected")
    if response.status_code == 403:
        return CredentialResult(valid=False, code="key_invalid_or_expired")
    if response.status_code != 200:
        logger.warning("Sharadar key check got HTTP %s", response.status_code)
        raise CredentialCheckUnavailable("the key check got an unexpected status")
    try:
        body = response.json()
    except ValueError:
        logger.warning("Sharadar key check got a response that is not JSON")
        raise CredentialCheckUnavailable("the key check got no JSON") from None
    datatable = body.get("datatable", {}) if isinstance(body, dict) else None
    if not isinstance(datatable, dict):
        logger.warning("Sharadar key check got a response without a datatable object")
        raise CredentialCheckUnavailable("the key check got an unexpected body")
    data = datatable.get("data", [])
    if not isinstance(data, list):
        logger.warning("Sharadar key check got a datatable without a row list")
        raise CredentialCheckUnavailable("the key check got an unexpected body")
    if not data:
        return CredentialResult(valid=False, code="no_subscription_data")
    return CredentialResult(valid=True, code=None)
