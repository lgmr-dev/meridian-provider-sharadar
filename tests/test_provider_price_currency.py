"""Price-currency rules of the provider, with only the Nasdaq transport replaced."""

from __future__ import annotations

import socket
from pathlib import Path

import nasdaqdatalink
import pandas as pd
import pytest

from meridian_provider_sharadar.provider import SharadarProvider, _cache_identity, _write_cache


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("currency regression attempted live network access")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(nasdaqdatalink.ApiConfig, "api_key", "test-key")


def price_metadata(ticker: str = "SPY", table: str | None = "SFP", currency: object = "USD") -> dict:
    return {"ticker": ticker, "table": table, "name": "Synthetic security", "currency": currency}


def install_transport(monkeypatch: pytest.MonkeyPatch, metadata: list[dict]) -> list[tuple[str, dict]]:
    calls = []

    def get_table(table: str, **query: object) -> pd.DataFrame:
        calls.append((table, query))
        if table == "SHARADAR/TICKERS":
            return pd.DataFrame(metadata)
        assert table in {"SHARADAR/SEP", "SHARADAR/SFP"}
        return pd.DataFrame(
            {
                "ticker": [query["ticker"]] * 3,
                "date": ["2024-01-31", "2024-02-29", "2024-03-28"],
                "close": [100.0, 101.0, 102.0],
                "closeadj": [99.0, 100.0, 102.0],
            }
        )

    monkeypatch.setattr(nasdaqdatalink, "get_table", get_table)
    return calls


@pytest.mark.parametrize("legacy_table", [None, "SF1"])
def test_price_currency_refreshes_legacy_metadata_without_assuming_usd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, legacy_table: str | None
) -> None:
    legacy = price_metadata(table=legacy_table, currency="DKK")
    _write_cache(str(tmp_path), "SPY_meta", {"meta": legacy}, identity=_cache_identity(table="TICKERS", ticker="SPY"))
    calls = install_transport(monkeypatch, [price_metadata()])
    provider = SharadarProvider(api_key="test-key", cache_dir=str(tmp_path))

    assert provider.get_price_currency("SPY") == "USD"
    assert len(calls) == 1
    assert provider.get_price_currency("SPY") == "USD"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        [price_metadata(table="SF1")],
        [price_metadata(table=None)],
        [price_metadata(ticker="WRONG")],
        [price_metadata(table="SEP"), price_metadata(table="SFP")],
    ],
)
def test_unproven_price_metadata_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, metadata: list[dict]
) -> None:
    install_transport(monkeypatch, metadata)
    provider = SharadarProvider(api_key="test-key", cache_dir=str(tmp_path))

    with pytest.raises(ValueError, match="currency"):
        provider.get_price_currency("SPY")


@pytest.mark.parametrize("currency", [None, "", "US", "USDD", "123", "€UR", 123])
def test_missing_or_malformed_currency_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, currency: object
) -> None:
    install_transport(monkeypatch, [price_metadata(currency=currency)])
    provider = SharadarProvider(api_key="test-key", cache_dir=str(tmp_path))

    with pytest.raises(ValueError, match="currency"):
        provider.get_price_currency("SPY")


def test_price_currency_is_stripped_and_upper_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_transport(
        monkeypatch,
        [
            price_metadata(ticker="SYNTH", table="SF1", currency="DKK"),
            price_metadata(ticker="SYNTH", table="SEP", currency=" eur "),
        ],
    )
    provider = SharadarProvider(api_key="test-key", cache_dir=str(tmp_path))

    assert provider.get_price_currency("SYNTH") == "EUR"
