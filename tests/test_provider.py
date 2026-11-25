"""Tests for SharadarProvider — prices (SEP/SFP), metadata (TICKERS),
dividends, validation, dividend-adjusted close, and CSV format."""

import json
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from meridian_provider_sharadar.provider import (
    SharadarProvider,
    _bare_ticker,
    _cache_identity,
    _cache_path,
    _prices_cache_key,
    _validate_price_rows,
    _validate_sf1_rows,
    _write_cache,
)


def _make_provider(tmp_path, api_key="test-key"):
    """Create an offline SharadarProvider with a fake Nasdaq client."""
    provider = SharadarProvider(api_key=api_key, cache_dir=str(tmp_path))
    api = MagicMock()
    api.get_table.return_value = pd.DataFrame()
    provider._init_api = MagicMock(return_value=api)
    provider.get_beta = MagicMock(return_value=1.0)
    return provider


def _price_csv(provider, ticker, directory, start="1990-01-01", *, force_refresh=False):
    """Write ``price_frame`` as the in-tree provider wrote its price CSV."""
    table = "SFP" if provider.is_etf(ticker) else "SEP"
    frame = provider.price_frame(ticker, table, start, force_refresh=force_refresh)
    path = Path(directory) / f"{ticker}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path)
    return str(path)


def _load_live_aapl_response_shape() -> dict:
    """Load the sanitized live-response shape without licensed financial data."""
    fixture = Path(__file__).parent / "fixtures" / "sharadar" / "aapl_live_response_shape_2026-09-02.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _forbid_live_network(monkeypatch):
    """Provider unit tests must never depend on live transport behavior."""
    attempts = []

    def record_attempt(*args, **kwargs):
        attempts.append((args, kwargs))
        raise OSError("live network disabled in Sharadar provider unit tests")

    monkeypatch.setattr(socket.socket, "connect", record_attempt)
    yield
    assert attempts == [], "Sharadar provider unit test attempted a live network connection"


def _write_meta_cache(tmp_path, ticker, meta, ttl_hours=168):
    """Pre-populate the TICKERS metadata cache for a ticker."""
    del ttl_hours
    key = f"{_bare_ticker(ticker)}_meta"
    normalized_meta = {"ticker": _bare_ticker(ticker), **meta}
    _write_cache(
        str(tmp_path),
        key,
        {"meta": normalized_meta},
        identity=_cache_identity(table="TICKERS", ticker=ticker),
    )


def _write_sf1_cache(tmp_path, ticker, rows):
    """Pre-populate the SF1 cache for a ticker."""
    key = f"{_bare_ticker(ticker)}_sf1"
    normalized_rows = [
        {
            "ticker": _bare_ticker(ticker),
            "dimension": "MRY",
            "reportperiod": row.get("reportperiod", row.get("calendardate")),
            **row,
        }
        for row in rows
    ]
    _write_cache(
        str(tmp_path),
        key,
        {"rows": normalized_rows},
        identity=_cache_identity(table="SF1", ticker=ticker, dimension="MRY"),
    )


def _write_prices_cache(tmp_path, ticker, table, rows, start="1990-01-01"):
    """Pre-populate the prices cache for a ticker."""
    key, _ = _prices_cache_key(ticker, table, start)
    normalized_rows = [{"ticker": _bare_ticker(ticker), **row} for row in rows]
    _write_cache(
        str(tmp_path),
        key,
        {"rows": normalized_rows},
        identity=_cache_identity(table=table, ticker=ticker, start=start),
    )


class TestRawProviderOrdering:
    def test_sf1_rows_must_arrive_newest_first(self):
        rows = [
            {"ticker": "AAPL", "dimension": "MRY", "reportperiod": "2023-12-31"},
            {"ticker": "AAPL", "dimension": "MRY", "reportperiod": "2024-12-31"},
        ]

        with pytest.raises(ValueError, match="not newest-first"):
            _validate_sf1_rows(rows, "AAPL")

    def test_price_rows_must_arrive_oldest_first(self):
        rows = [
            {"ticker": "AAPL", "date": "2024-01-03", "close": 101.0, "closeadj": 101.0},
            {"ticker": "AAPL", "date": "2024-01-02", "close": 100.0, "closeadj": 100.0},
        ]

        with pytest.raises(ValueError, match="not oldest-first"):
            _validate_price_rows(rows, "AAPL", "1990-01-01", "SEP")

    def test_price_rows_reject_next_day_future_skew(self):
        future_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        rows = [{"ticker": "AAPL", "date": future_date, "close": 100.0, "closeadj": 100.0}]

        with pytest.raises(ValueError, match="outside the requested window"):
            _validate_price_rows(rows, "AAPL", "1990-01-01", "SEP")

    @pytest.mark.parametrize("missing_date", [None, pd.NaT, "NaT"])
    def test_price_rows_reject_missing_or_nat_dates(self, missing_date):
        rows = [{"ticker": "AAPL", "date": missing_date, "close": 100.0, "closeadj": 100.0}]

        with pytest.raises(ValueError, match="invalid price date"):
            _validate_price_rows(rows, "AAPL", "1990-01-01", "SEP")


class TestLiveResponseShapeNormalization:
    def test_sanitized_aapl_shape_reaches_strict_caches(self, tmp_path):
        snapshot = _load_live_aapl_response_shape()
        provider = _make_provider(tmp_path)
        provider._init_api.return_value.get_table.side_effect = [
            pd.DataFrame(snapshot["tickers"]),
            pd.DataFrame(snapshot["sf1"]),
            pd.DataFrame(snapshot["sep"]),
        ]

        meta = provider._fetch_tickers_meta("AAPL")
        fundamentals = provider._fetch_sf1("AAPL")
        prices = provider._fetch_prices("AAPL", "SEP", start="2024-01-01")

        assert meta["table"] == "SEP"
        assert fundamentals[0]["reportperiod"] == "2024-12-31"
        assert fundamentals[0]["epsdil"] is None
        assert prices["date"].tolist() == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
        for cache_path in (_cache_path(str(tmp_path), key) for key in ("AAPL_meta", "AAPL_sf1")):
            assert "NaN" not in cache_path.read_text(encoding="utf-8")
        price_key, _ = _prices_cache_key("AAPL", "SEP", "2024-01-01")
        assert "NaN" not in _cache_path(str(tmp_path), price_key).read_text(encoding="utf-8")

    def test_multiple_price_metadata_rows_fail_closed(self, tmp_path):
        snapshot = _load_live_aapl_response_shape()
        duplicate_price_row = {**snapshot["tickers"][-1], "table": "SFP"}
        provider = _make_provider(tmp_path)
        provider._init_api.return_value.get_table.return_value = pd.DataFrame(
            [*snapshot["tickers"], duplicate_price_row]
        )

        assert provider._fetch_tickers_meta("AAPL") is None
        assert not _cache_path(str(tmp_path), "AAPL_meta").exists()


class TestBareTickerHelper:
    def test_strips_sa_suffix(self):
        assert _bare_ticker("BOVA11.SA") == "BOVA11"

    def test_no_suffix_unchanged(self):
        assert _bare_ticker("AAPL") == "AAPL"

    @pytest.mark.parametrize("ticker", ["BRK.B", "BF.B"])
    def test_preserves_canonical_class_share_separator(self, ticker):
        assert _bare_ticker(ticker) == ticker

    def test_class_share_identity_reaches_every_sharadar_table(self, tmp_path):
        provider = _make_provider(tmp_path)

        provider._fetch_sf1("BRK.B")
        provider._fetch_tickers_meta("BRK.B")
        provider._fetch_prices("BRK.B", "SEP", start="1990-01-01")

        assert provider._init_api.return_value.get_table.call_args_list == [
            call("SHARADAR/SF1", ticker="BRK.B", dimension="MRY", paginate=True),
            call("SHARADAR/TICKERS", ticker="BRK.B", paginate=True),
            call(
                "SHARADAR/SEP",
                ticker="BRK.B",
                date={"gte": "1990-01-01"},
                paginate=True,
            ),
        ]

    @pytest.mark.parametrize("ticker", ["AAPL.SA", "BRK.SA.B", "FOO.SA.SA"])
    def test_rejects_unsupported_or_ambiguous_sa_identity(self, ticker):
        with pytest.raises(ValueError, match="Stock Selection|Sharadar"):
            _bare_ticker(ticker)


class TestIsETF:
    def test_etf_by_category(self, tmp_path):
        _write_meta_cache(tmp_path, "SPY", {"category": "ETF"})
        p = _make_provider(tmp_path)
        assert p.is_etf("SPY") is True

    def test_etf_by_table(self, tmp_path):
        _write_meta_cache(tmp_path, "GLD", {"category": "Domestic", "table": "SFP"})
        p = _make_provider(tmp_path)
        assert p.is_etf("GLD") is True

    def test_stock_not_etf(self, tmp_path):
        _write_meta_cache(
            tmp_path,
            "AAPL",
            {
                "category": "Domestic Common Stock",
                "table": "SEP",
            },
        )
        p = _make_provider(tmp_path)
        assert p.is_etf("AAPL") is False

    def test_missing_meta_not_etf(self, tmp_path):
        """An empty provider response identifies no ETF without live I/O."""
        p = _make_provider(tmp_path)
        assert p.is_etf("UNKNOWN") is False
        p._init_api.return_value.get_table.assert_called_once_with(
            "SHARADAR/TICKERS",
            ticker="UNKNOWN",
            paginate=True,
        )


class TestGetTickerInfo:
    def test_builds_yfinance_compatible_dict(self, tmp_path):
        _write_meta_cache(
            tmp_path,
            "MSFT",
            {
                "name": "Microsoft Corp",
                "sector": "Technology",
                "sicindustry": "Services-Prepackaged Software",
                "category": "Domestic Common Stock",
                "exchange": "NASDAQ",
                "currency": "USD",
            },
        )
        _write_sf1_cache(
            tmp_path,
            "MSFT",
            [
                {"calendardate": "2024-12-31", "sharesbas": 7500000000, "price": 420.0},
            ],
        )
        p = _make_provider(tmp_path)
        info = p.get_ticker_info("MSFT")

        assert info["shortName"] == "Microsoft Corp"
        assert info["sector"] == "Technology"
        assert info["industry"] == "Services-Prepackaged Software"
        assert info["quoteType"] == "EQUITY"
        assert info["currency"] == "USD"
        assert info["financialCurrency"] == "USD"
        assert info["sharesOutstanding"] == 7500000000
        assert info["marketCap"] == pytest.approx(420.0 * 7500000000)
        assert "beta" not in info
        p.get_beta.assert_not_called()

        valuation_info = p.get_ticker_info("MSFT", include_beta=True)

        assert valuation_info["beta"] == pytest.approx(1.0)
        p.get_beta.assert_called_once_with("MSFT")

    def test_etf_quote_type(self, tmp_path):
        _write_meta_cache(
            tmp_path,
            "SPY",
            {
                "name": "Example Index Fund",
                "category": "ETF",
            },
        )
        _write_sf1_cache(tmp_path, "SPY", [])
        p = _make_provider(tmp_path)
        info = p.get_ticker_info("SPY")
        assert info["quoteType"] == "ETF"
        assert "financialCurrency" not in info

    def test_sfp_table_is_authoritative_fund_identity(self, tmp_path):
        p = _make_provider(tmp_path)
        p._fetch_tickers_meta = MagicMock(
            return_value={
                "name": "Synthetic Fund",
                "category": "Domestic",
                "table": "SFP",
                "currency": "USD",
            }
        )
        p._fetch_sf1 = MagicMock(side_effect=AssertionError("SFP fund must not fetch SF1 equity fundamentals"))
        p.get_beta = MagicMock(side_effect=AssertionError("SFP fund must not derive SEP equity beta"))

        info = p.get_ticker_info("SYNTH")

        assert p.is_etf("SYNTH") is True
        assert info["quoteType"] == "ETF"

    @pytest.mark.parametrize(
        ("category", "table"),
        [
            ("Domestic Common Stock", "SFP"),
            ("ETF", "SEP"),
        ],
    )
    def test_conflicting_table_and_category_fail_closed(self, tmp_path, category, table):
        p = _make_provider(tmp_path)
        p._fetch_tickers_meta = MagicMock(return_value={"name": "Contradictory", "category": category, "table": table})

        with pytest.raises(ValueError, match="conflicting asset types"):
            p.get_ticker_info("SYNTH")

    def test_live_conflicting_metadata_fails_at_ingestion_boundary(self, tmp_path):
        p = _make_provider(tmp_path)
        p._init_api.return_value.get_table.return_value = pd.DataFrame(
            [{"ticker": "SPY", "name": "Contradictory", "category": "ETF", "table": "SEP"}]
        )

        with pytest.raises(ValueError, match="conflicting asset types"):
            p._fetch_tickers_meta("SPY")

    def test_missing_meta_returns_minimal(self, tmp_path):
        p = _make_provider(tmp_path)
        info = p.get_ticker_info("NONEXISTENT")
        assert info == {
            "shortName": "NONEXISTENT",
            "longName": "NONEXISTENT",
            "quoteType": "EQUITY",
        }
        assert p._init_api.return_value.get_table.call_args_list == [
            call("SHARADAR/TICKERS", ticker="NONEXISTENT", paginate=True),
            call("SHARADAR/SF1", ticker="NONEXISTENT", dimension="MRY", paginate=True),
        ]


class TestValidateTicker:
    def test_valid_ticker(self, tmp_path):
        _write_meta_cache(tmp_path, "AAPL", {"name": "Apple Inc", "isdelisted": "N"})
        p = _make_provider(tmp_path)
        result = p.validate_ticker("AAPL")
        assert result["valid"] is True
        assert result["name"] == "Apple Inc"

    def test_delisted_ticker(self, tmp_path):
        _write_meta_cache(tmp_path, "OLD", {"name": "Old Corp", "isdelisted": "Y"})
        p = _make_provider(tmp_path)
        result = p.validate_ticker("OLD")
        assert result["valid"] is False
        assert result["name"] == "Old Corp"

    def test_nonexistent_ticker(self, tmp_path):
        p = _make_provider(tmp_path)
        result = p.validate_ticker("ZZZZZ")
        assert result["valid"] is False
        assert result["name"] is None
        p._init_api.return_value.get_table.assert_called_once_with(
            "SHARADAR/TICKERS",
            ticker="ZZZZZ",
            paginate=True,
        )

    @pytest.mark.parametrize("name", [{"provider": "object"}, "x" * 201, "Apple\nInc"])
    def test_malformed_cached_provider_name_is_rejected(self, tmp_path, name):
        _write_meta_cache(tmp_path, "AAPL", {"name": name, "isdelisted": "N"})
        p = _make_provider(tmp_path)

        result = p.validate_ticker("AAPL")

        assert result == {"valid": False, "name": None}
        p._init_api.return_value.get_table.assert_called_once_with(
            "SHARADAR/TICKERS",
            ticker="AAPL",
            paginate=True,
        )


class TestDownloadPricesCSV:
    def test_invalid_price_table_fails_before_cache_or_provider_access(self, tmp_path):
        provider = _make_provider(tmp_path)

        with pytest.raises(ValueError, match="Unsupported Sharadar price table"):
            provider._fetch_prices("AAPL", "../../SEP")

        provider._init_api.assert_not_called()

    def test_future_live_price_response_is_not_cached(self, tmp_path):
        future_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        provider = _make_provider(tmp_path)
        provider._init_api.return_value.get_table.return_value = pd.DataFrame(
            [{"ticker": "AAPL", "date": future_date, "close": 100.0, "closeadj": 100.0}]
        )

        with pytest.raises(ValueError, match="outside the requested window"):
            provider._fetch_prices("AAPL", "SEP", start="1990-01-01")

        key, _ = _prices_cache_key("AAPL", "SEP", "1990-01-01")
        assert not _cache_path(str(tmp_path), key).exists()

    @pytest.mark.parametrize("missing_date", [None, pd.NaT, "NaT"])
    def test_missing_live_price_date_is_not_cached(self, tmp_path, missing_date):
        provider = _make_provider(tmp_path)
        provider._init_api.return_value.get_table.return_value = pd.DataFrame(
            [{"ticker": "AAPL", "date": missing_date, "close": 100.0, "closeadj": 100.0}]
        )

        with pytest.raises(ValueError, match="invalid price date"):
            provider._fetch_prices("AAPL", "SEP", start="1990-01-01")

        key, _ = _prices_cache_key("AAPL", "SEP", "1990-01-01")
        assert not _cache_path(str(tmp_path), key).exists()

    @pytest.mark.parametrize("missing_date", [None, "NaT"])
    def test_missing_cached_price_date_is_rejected(self, tmp_path, missing_date):
        _write_prices_cache(
            tmp_path,
            "AAPL",
            "SEP",
            [{"date": missing_date, "close": 100.0, "closeadj": 100.0}],
        )

        with pytest.raises(ValueError, match="invalid price date"):
            _make_provider(tmp_path)._fetch_prices("AAPL", "SEP", start="1990-01-01")

    def test_future_cached_price_response_is_rejected_without_csv(self, tmp_path):
        future_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        _write_meta_cache(tmp_path, "AAPL", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_prices_cache(
            tmp_path,
            "AAPL",
            "SEP",
            [{"date": future_date, "close": 100.0, "closeadj": 100.0}],
        )
        output_dir = tmp_path / "out"

        with pytest.raises(ValueError, match="outside the requested window"):
            _price_csv(_make_provider(tmp_path), "AAPL", str(output_dir))

    def test_forced_refresh_bypasses_fresh_price_cache(self, tmp_path):
        _write_meta_cache(tmp_path, "AAPL", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_prices_cache(
            tmp_path,
            "AAPL",
            "SEP",
            [{"date": "2024-01-02", "close": 100.0, "closeadj": 100.0}],
        )
        provider = _make_provider(tmp_path)
        api = MagicMock()
        api.get_table.return_value = pd.DataFrame(
            [{"ticker": "AAPL", "date": "2024-01-03", "close": 101.0, "closeadj": 101.0}]
        )
        provider._init_api = MagicMock(return_value=api)

        path = _price_csv(
            provider,
            "AAPL",
            str(tmp_path / "out"),
            force_refresh=True,
        )

        result = pd.read_csv(path)
        assert result["Date"].iloc[0] == "2024-01-03"
        assert result["Adj Close"].iloc[0] == 101.0
        api.get_table.assert_called_once_with(
            "SHARADAR/SEP",
            ticker="AAPL",
            date={"gte": "1990-01-01"},
            paginate=True,
        )

    def test_price_cache_identity_includes_requested_history_start(self, tmp_path):
        _write_prices_cache(
            tmp_path,
            "AAPL",
            "SEP",
            [{"date": "2024-01-02", "close": 100.0, "closeadj": 100.0}],
            start="2020-01-01",
        )
        provider = _make_provider(tmp_path)
        api = MagicMock()
        api.get_table.return_value = pd.DataFrame(
            [{"ticker": "AAPL", "date": "1995-01-03", "close": 10.0, "closeadj": 10.0}]
        )
        provider._init_api = MagicMock(return_value=api)

        result = provider._fetch_prices("AAPL", "SEP", start="1990-01-01")

        assert result["date"].iloc[0] == pd.Timestamp("1995-01-03")
        api.get_table.assert_called_once_with(
            "SHARADAR/SEP",
            ticker="AAPL",
            date={"gte": "1990-01-01"},
            paginate=True,
        )

    def test_new_price_window_deletes_only_unreadable_windows_of_the_same_query_family(self, tmp_path):
        now = datetime.now(timezone.utc)
        cache_root = tmp_path / "cache" / "sharadar"

        def window(ticker, table, start, fetched_hours_ago, modified_hours_ago=None):
            key, normalized = _prices_cache_key(ticker, table, start)
            fetched = now - timedelta(hours=fetched_hours_ago)
            _write_cache(
                str(tmp_path),
                key,
                {"rows": [{"ticker": ticker, "date": normalized, "close": 1.0, "closeadj": 1.0}]},
                identity=_cache_identity(table=table, ticker=ticker, start=normalized),
                source_fetched_at=fetched.isoformat(),
            )
            path = cache_root / f"{key.upper()}.json"
            modified = (now - timedelta(hours=modified_hours_ago or fetched_hours_ago)).timestamp()
            os.utime(path, (modified, modified))
            return path.name

        expired = window("AAPL", "SEP", "2016-01-01", 25)
        kept = {
            window("AAPL", "SEP", "2017-01-01", 23),
            window("AAPL", "SEP", "2018-01-01", 1, modified_hours_ago=30),
            window("AAPL", "SFP", "2016-01-01", 25),
            window("AAP", "SEP", "2016-01-01", 25),
        }
        provider = _make_provider(tmp_path)
        provider._init_api.return_value.get_table.return_value = pd.DataFrame(
            [{"ticker": "AAPL", "date": "2020-01-02", "close": 101.0, "closeadj": 101.0}]
        )

        provider._fetch_prices("AAPL", "SEP", start="2020-01-01")

        assert expired not in {path.name for path in cache_root.iterdir()}
        assert {path.name for path in cache_root.glob("*_PRICES_*.json")} == kept | {"AAPL_PRICES_SEP_20200101.json"}

    def test_cleanup_keeps_a_window_that_another_writer_just_replaced(self, tmp_path, monkeypatch):
        cache_root = tmp_path / "cache" / "sharadar"
        old_key, old_start = _prices_cache_key("AAPL", "SEP", "2016-01-01")
        expired = f"{old_key.upper()}.json"
        _write_cache(
            str(tmp_path),
            old_key,
            {"rows": [{"ticker": "AAPL", "date": old_start, "close": 1.0, "closeadj": 1.0}]},
            identity=_cache_identity(table="SEP", ticker="AAPL", start=old_start),
            source_fetched_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
        )
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).timestamp()
        os.utime(cache_root / expired, (stale, stale))

        cleaner = _make_provider(tmp_path)
        cleaner._init_api.return_value.get_table.return_value = pd.DataFrame(
            [{"ticker": "AAPL", "date": "2020-01-02", "close": 101.0, "closeadj": 101.0}]
        )
        writer = _make_provider(tmp_path)
        fetched = threading.Event()

        def fetch_replacement(*_args, **_kwargs):
            fetched.set()
            return pd.DataFrame([{"ticker": "AAPL", "date": "2016-01-04", "close": 99.0, "closeadj": 99.0}])

        writer._init_api.return_value.get_table.side_effect = fetch_replacement
        replacement = threading.Thread(
            target=lambda: writer._fetch_prices("AAPL", "SEP", start="2016-01-01", force_refresh=True)
        )
        read_text = Path.read_text

        def replace_after_the_cleanup_reads(path, *args, **kwargs):
            text = read_text(path, *args, **kwargs)
            if path.name == expired and not replacement.is_alive() and replacement.ident is None:
                # The other writer replaces the window between the read and the delete.
                # The wait starts after its fetch, so a slow fetch cannot hide the race.
                replacement.start()
                assert fetched.wait(timeout=10)
                replacement.join(timeout=0.5)
            return text

        monkeypatch.setattr(Path, "read_text", replace_after_the_cleanup_reads)
        cleaner._fetch_prices("AAPL", "SEP", start="2020-01-01")
        replacement.join()
        monkeypatch.undo()

        envelope = json.loads((cache_root / expired).read_text(encoding="utf-8"))
        assert envelope["payload"]["rows"][0]["close"] == 99.0

    def test_invalid_price_history_start_fails_at_boundary(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid Sharadar price-history start date"):
            _make_provider(tmp_path)._fetch_prices("AAPL", "SEP", start="not-a-date")

    def test_equity_uses_sep(self, tmp_path):
        """Non-ETF ticker routes to SEP table."""
        _write_meta_cache(tmp_path, "AAPL", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_sf1_cache(tmp_path, "AAPL", [])
        _write_prices_cache(
            tmp_path,
            "AAPL",
            "SEP",
            [
                {
                    "date": "2024-01-02",
                    "open": 185.0,
                    "high": 186.0,
                    "low": 184.0,
                    "close": 185.5,
                    "closeadj": 185.5,
                    "volume": 50000000,
                },
                {
                    "date": "2024-01-03",
                    "open": 186.0,
                    "high": 187.0,
                    "low": 185.0,
                    "close": 186.5,
                    "closeadj": 186.5,
                    "volume": 45000000,
                },
            ],
        )
        p = _make_provider(tmp_path)
        data_dir = str(tmp_path / "data_out")
        path = _price_csv(p, "AAPL", data_dir)

        assert os.path.exists(path)
        df = pd.read_csv(path)
        assert "Date" in df.columns
        assert "Open" in df.columns
        assert "Close" in df.columns
        assert "Adj Close" in df.columns
        assert "Volume" in df.columns
        assert len(df) == 2

    def test_etf_uses_sfp(self, tmp_path):
        """ETF ticker routes to SFP table."""
        _write_meta_cache(tmp_path, "SPY", {"category": "ETF", "table": "SFP"})
        _write_sf1_cache(tmp_path, "SPY", [])
        _write_prices_cache(
            tmp_path,
            "SPY",
            "SFP",
            [
                {
                    "date": "2024-01-02",
                    "open": 470.0,
                    "high": 472.0,
                    "low": 469.0,
                    "close": 471.0,
                    "closeadj": 471.0,
                    "volume": 80000000,
                },
            ],
        )
        p = _make_provider(tmp_path)
        data_dir = str(tmp_path / "data_out")
        path = _price_csv(p, "SPY", data_dir)

        df = pd.read_csv(path)
        assert len(df) == 1
        assert "Adj Close" in df.columns

    def test_csv_format_matches_yfinance(self, tmp_path):
        """Output CSV has exact columns expected by engine.load_data()."""
        _write_meta_cache(tmp_path, "GLD", {"category": "ETF", "table": "SFP"})
        _write_sf1_cache(tmp_path, "GLD", [])
        _write_prices_cache(
            tmp_path,
            "GLD",
            "SFP",
            [
                {
                    "date": "2024-01-02",
                    "open": 190.0,
                    "high": 191.0,
                    "low": 189.0,
                    "close": 190.5,
                    "closeadj": 190.5,
                    "volume": 10000000,
                },
            ],
        )
        p = _make_provider(tmp_path)
        data_dir = str(tmp_path / "data_out")
        path = _price_csv(p, "GLD", data_dir)

        df = pd.read_csv(path)
        expected_cols = {"Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"}
        assert set(df.columns) == expected_cols

    def test_closeadj_column_used_when_available(self, tmp_path):
        """If closeadj is in the data, use it as Adj Close."""
        _write_meta_cache(tmp_path, "AAPL", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_sf1_cache(tmp_path, "AAPL", [])
        _write_prices_cache(
            tmp_path,
            "AAPL",
            "SEP",
            [
                {
                    "date": "2024-01-02",
                    "open": 185.0,
                    "high": 186.0,
                    "low": 184.0,
                    "close": 185.5,
                    "closeadj": 180.0,
                    "volume": 50000000,
                },
            ],
        )
        p = _make_provider(tmp_path)
        data_dir = str(tmp_path / "data_out")
        path = _price_csv(p, "AAPL", data_dir)

        df = pd.read_csv(path)
        assert df["Adj Close"].iloc[0] == pytest.approx(180.0)


class TestDividendAdjustedClose:
    def test_missing_official_adjusted_close_fails_loud(self, tmp_path):
        _write_sf1_cache(tmp_path, "NODIV", [])
        p = _make_provider(tmp_path)
        prices = pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "close": [100.0, 101.0],
            }
        )
        with pytest.raises(ValueError, match="annual SF1 dividends cannot reconstruct"):
            p._compute_dividend_adjusted_close(prices, "NODIV")

    def test_annual_dividend_data_does_not_authorize_synthetic_ex_date(self, tmp_path):
        _write_sf1_cache(
            tmp_path,
            "DIV",
            [
                {
                    "calendardate": "2024-06-30",
                    "ncfdiv": -200000000,
                    "shareswa": 100000000,
                },
            ],
        )
        p = _make_provider(tmp_path)
        prices = pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02", "2024-06-30", "2024-12-30"]),
                "close": [100.0, 100.0, 100.0],
            }
        )
        with pytest.raises(ValueError, match="annual SF1 dividends cannot reconstruct"):
            p._compute_dividend_adjusted_close(prices, "DIV")

    def test_download_without_official_adjusted_close_writes_no_file(self, tmp_path):
        _write_meta_cache(tmp_path, "MISS", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_prices_cache(tmp_path, "MISS", "SEP", [{"date": "2024-01-02", "close": 100.0}])
        output_dir = tmp_path / "out"

        with pytest.raises(ValueError, match="annual SF1 dividends cannot reconstruct"):
            _price_csv(_make_provider(tmp_path), "MISS", str(output_dir))

    def test_download_rejects_partial_adjusted_close(self, tmp_path):
        _write_meta_cache(tmp_path, "PART", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_prices_cache(
            tmp_path,
            "PART",
            "SEP",
            [
                {"date": "2024-01-02", "close": 100.0, "closeadj": 99.0},
                {"date": "2024-01-03", "close": 101.0, "closeadj": None},
            ],
        )

        with pytest.raises(ValueError, match="incomplete adjusted close"):
            _price_csv(_make_provider(tmp_path), "PART", str(tmp_path / "out"))

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            ({"date": "2024-01-02", "close": 0.0, "closeadj": 99.0}, "non-positive price-only close"),
            ({"date": "2024-01-02", "close": 100.0, "closeadj": 0.0}, "non-positive adjusted close"),
        ],
    )
    def test_download_rejects_non_positive_price_levels(self, tmp_path, row, message):
        _write_meta_cache(tmp_path, "ZERO", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_prices_cache(tmp_path, "ZERO", "SEP", [row])

        with pytest.raises(ValueError, match=message):
            _price_csv(_make_provider(tmp_path), "ZERO", str(tmp_path / "out"))

    def test_close_is_used_for_price_only_close_even_when_closeunadj_is_present(self, tmp_path):
        _write_meta_cache(tmp_path, "RAW", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_prices_cache(
            tmp_path,
            "RAW",
            "SEP",
            [{"date": "2024-01-02", "close": 99.0, "closeunadj": 101.0, "closeadj": 98.0}],
        )

        path = _price_csv(_make_provider(tmp_path), "RAW", str(tmp_path / "out"))
        frame = pd.read_csv(path)
        assert frame["Close"].iloc[0] == 99.0
        assert frame["Adj Close"].iloc[0] == 98.0

    def test_closeunadj_without_official_price_only_close_fails_loud(self, tmp_path):
        _write_meta_cache(tmp_path, "RAW", {"category": "Domestic Common Stock", "table": "SEP"})
        _write_prices_cache(
            tmp_path,
            "RAW",
            "SEP",
            [{"date": "2024-01-02", "closeunadj": 101.0, "closeadj": 98.0}],
        )

        with pytest.raises(ValueError, match="no split-adjusted price-only close data"):
            _price_csv(_make_provider(tmp_path), "RAW", str(tmp_path / "out"))


class TestGetDividends:
    def test_annual_dps_from_sf1(self, tmp_path):
        """Dividend per share computed from ncfdiv / shareswa."""
        _write_sf1_cache(
            tmp_path,
            "JNJ",
            [
                {"calendardate": "2024-12-31", "ncfdiv": -5000000000, "shareswa": 2500000000},
                {"calendardate": "2023-12-31", "ncfdiv": -4500000000, "shareswa": 2500000000},
            ],
        )
        p = _make_provider(tmp_path)
        divs = p.get_dividends("JNJ")

        assert len(divs) == 2
        assert divs.iloc[-1] == pytest.approx(2.0)
        assert divs.iloc[0] == pytest.approx(1.8)

    def test_no_data_returns_empty(self, tmp_path):
        _write_sf1_cache(tmp_path, "NODIV", [])
        p = _make_provider(tmp_path)
        divs = p.get_dividends("NODIV")
        assert len(divs) == 0

    def test_zero_dividend_included(self, tmp_path):
        """Zero ncfdiv → DPS=0 still included."""
        _write_sf1_cache(
            tmp_path,
            "GROWTH",
            [
                {"calendardate": "2024-12-31", "ncfdiv": 0, "shareswa": 1000000000},
            ],
        )
        p = _make_provider(tmp_path)
        divs = p.get_dividends("GROWTH")
        assert len(divs) == 1
        assert divs.iloc[0] == pytest.approx(0.0)


class TestCacheKeyScheme:
    @pytest.mark.parametrize(
        "key",
        ["../../../outside", "/tmp/outside", "AAPL/../../outside", ".hidden", "A" * 201],
    )
    def test_cache_path_rejects_uncontained_or_invalid_keys(self, tmp_path, key):
        with pytest.raises(ValueError, match="Invalid Sharadar cache key"):
            _cache_path(str(tmp_path), key)

    def test_sf1_cache_key(self, tmp_path):
        """SF1 data cached under {TICKER}_sf1 key."""
        _write_sf1_cache(tmp_path, "AAPL", [{"calendardate": "2024-12-31"}])
        path = _cache_path(str(tmp_path), "AAPL_sf1")
        assert path.exists()

    def test_cache_key_accepts_supported_caret_ticker_identity(self, tmp_path):
        assert _cache_path(str(tmp_path), "^TNX_meta").name == "^TNX_META.json"

    def test_cache_writer_rejects_non_rfc_nan_payload(self, tmp_path):
        key = "AAPL_meta"

        with pytest.raises(ValueError, match="JSON compliant"):
            _write_cache(
                str(tmp_path),
                key,
                {"meta": {"beta": float("nan")}},
                identity=_cache_identity(table="TICKERS", ticker="AAPL"),
            )

        assert not _cache_path(str(tmp_path), key).exists()

    def test_meta_cache_key(self, tmp_path):
        """TICKERS data cached under {TICKER}_meta key."""
        _write_meta_cache(tmp_path, "AAPL", {"name": "Apple"})
        path = _cache_path(str(tmp_path), "AAPL_meta")
        assert path.exists()

    def test_prices_cache_key(self, tmp_path):
        """Price cache identity includes the requested history start."""
        _write_prices_cache(tmp_path, "AAPL", "SEP", [])
        key, _ = _prices_cache_key("AAPL", "SEP", "1990-01-01")
        path = _cache_path(str(tmp_path), key)
        assert path.exists()

    def test_no_collision_between_types(self, tmp_path):
        """Different data types for same ticker don't collide."""
        _write_sf1_cache(tmp_path, "MSFT", [{"calendardate": "2024-12-31"}])
        _write_meta_cache(tmp_path, "MSFT", {"name": "Microsoft"})
        _write_prices_cache(tmp_path, "MSFT", "SEP", [])

        sf1_path = _cache_path(str(tmp_path), "MSFT_sf1")
        meta_path = _cache_path(str(tmp_path), "MSFT_meta")
        prices_key, _ = _prices_cache_key("MSFT", "SEP", "1990-01-01")
        prices_path = _cache_path(str(tmp_path), prices_key)

        assert sf1_path.exists()
        assert meta_path.exists()
        assert prices_path.exists()
        assert sf1_path != meta_path != prices_path


class TestProviderRequiresApiKey:
    def test_missing_key_raises(self, tmp_path):
        """SharadarProvider without API key raises ValueError."""
        with (
            patch.dict(os.environ, {"SHARADAR_API_KEY": ""}, clear=False),
            pytest.raises(ValueError, match="API key required"),
        ):
            SharadarProvider(api_key="", cache_dir=str(tmp_path))

    def test_nasdaq_transport_has_finite_timeout(self, tmp_path):
        from nasdaqdatalink.connection import Connection

        from meridian_provider_sharadar.provider import SHARADAR_HTTP_TIMEOUT

        provider = SharadarProvider(api_key="test-key", cache_dir=str(tmp_path))
        provider._init_api()

        response = MagicMock(status_code=200)
        session = MagicMock()
        session.request.return_value = response
        with patch.object(Connection, "get_session", return_value=session):
            Connection.execute_request("get", "https://example.invalid", timeout=None)

        assert session.request.call_args.kwargs["timeout"] == SHARADAR_HTTP_TIMEOUT


class TestFiscalPeriodIdentity:
    def test_financial_columns_use_reportperiod_not_filing_date(self, tmp_path):
        provider = _make_provider(tmp_path)
        provider._init_api.return_value.get_table.return_value = pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "dimension": "MRY",
                    "reportperiod": "2024-12-31",
                    "datekey": "2025-02-15",
                    "revenue": 100.0,
                }
            ]
        )

        frame = provider.get_financials("AAPL")

        assert list(frame.columns) == [pd.Timestamp("2024-12-31")]

    def test_missing_reportperiod_fails_closed(self, tmp_path):
        provider = _make_provider(tmp_path)
        provider._init_api.return_value.get_table.return_value = pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "dimension": "MRY",
                    "calendardate": "2024-12-31",
                    "datekey": "2025-02-15",
                    "revenue": 100.0,
                }
            ]
        )

        assert provider.get_financials("AAPL").empty

    def test_duplicate_cached_fiscal_periods_are_rejected(self, tmp_path):
        _write_sf1_cache(
            tmp_path,
            "AAPL",
            [
                {"reportperiod": "2024-12-31", "datekey": "2025-02-01", "revenue": 100.0},
                {"reportperiod": "2024-12-31", "datekey": "2025-03-01", "revenue": 101.0},
            ],
        )
        provider = _make_provider(tmp_path)

        assert provider.get_financials("AAPL").empty
        provider._init_api.return_value.get_table.assert_called_once()

    def test_future_live_fiscal_period_is_rejected(self, tmp_path):
        from datetime import timedelta

        provider = _make_provider(tmp_path)
        future_period = (datetime.now(timezone.utc) + timedelta(days=365)).date().isoformat()
        provider._init_api.return_value.get_table.return_value = pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "dimension": "MRY",
                    "reportperiod": future_period,
                    "datekey": future_period,
                    "revenue": 100.0,
                }
            ]
        )

        assert provider.get_financials("AAPL").empty


def _make_sf1_row(date="2024-12-31", **overrides):
    """Create a complete SF1 row with sensible defaults."""
    row = {
        "reportperiod": date,
        "calendardate": date,
        "revenue": 1_200_000_000,
        "netinc": 120_000_000,
        "netinccmn": 120_000_000,
        "gp": 500_000_000,
        "opinc": 240_000_000,
        "ebitda": 294_000_000,
        "ebit": 240_000_000,
        "eps": 5.0,
        "epsdil": 4.8,
        "shareswa": 25_000_000,
        "shareswadil": 26_000_000,
        "taxexp": 36_000_000,
        "ebt": 228_000_000,
        "assets": 2_000_000_000,
        "equity": 900_000_000,
        "debt": 460_000_000,
        "debtnc": 360_000_000,
        "assetsc": 640_000_000,
        "liabilitiesc": 280_000_000,
        "cashneq": 170_000_000,
        "sharesbas": 25_000_000,
        "investmentsc": 30_000_000,
        "goodwill": 100_000_000,
        "intangibles": 50_000_000,
        "ncfo": 270_000_000,
        "capex": -90_000_000,
        "fcf": 180_000_000,
        "depamor": 54_000_000,
        "ncfdiv": -50_000_000,
        "ncfdebt": 20_000_000,
    }
    row.update(overrides)
    return row


class TestBuildDf:
    """Tests for _build_df() — converts SF1 rows to yfinance-compatible DataFrames."""

    def test_income_stmt_has_all_13_keys(self):
        from meridian_provider_sharadar.provider import INCOME_STMT_MAP, _build_df

        rows = [_make_sf1_row()]
        df = _build_df(rows, INCOME_STMT_MAP)

        assert df.shape[0] == 13
        for yf_key in INCOME_STMT_MAP:
            assert yf_key in df.index, f"Missing key: {yf_key}"

        col = df.columns[0]
        assert df.loc["Total Revenue", col] == 1_200_000_000
        assert df.loc["Net Income", col] == 120_000_000
        assert df.loc["Basic EPS", col] == 5.0

    def test_balance_sheet_has_all_keys(self):
        from meridian_provider_sharadar.provider import BALANCE_SHEET_MAP, _build_df

        rows = [_make_sf1_row()]
        df = _build_df(rows, BALANCE_SHEET_MAP)

        assert df.shape[0] == len(BALANCE_SHEET_MAP)
        for yf_key in BALANCE_SHEET_MAP:
            assert yf_key in df.index, f"Missing key: {yf_key}"

        col = df.columns[0]
        assert df.loc["Cash And Cash Equivalents", col] == 170_000_000
        assert df.loc["Cash Cash Equivalents And Short Term Investments", col] == 170_000_000

    def test_cashflow_has_all_keys(self):
        from meridian_provider_sharadar.provider import CASHFLOW_MAP, _build_df

        rows = [_make_sf1_row()]
        df = _build_df(rows, CASHFLOW_MAP)

        assert df.shape[0] == len(CASHFLOW_MAP)
        for yf_key in CASHFLOW_MAP:
            assert yf_key in df.index, f"Missing key: {yf_key}"

        col = df.columns[0]
        assert df.loc["Net Debt Issuance", col] == 20_000_000
        assert df.loc["Capital Expenditure", col] == -90_000_000

    def test_dates_are_timestamps(self):
        from meridian_provider_sharadar.provider import INCOME_STMT_MAP, _build_df

        rows = [_make_sf1_row()]
        df = _build_df(rows, INCOME_STMT_MAP)

        for col in df.columns:
            assert isinstance(col, pd.Timestamp), f"Expected Timestamp, got {type(col)}"

    def test_index_is_yfinance_key_names(self):
        from meridian_provider_sharadar.provider import INCOME_STMT_MAP, _build_df

        rows = [_make_sf1_row()]
        df = _build_df(rows, INCOME_STMT_MAP)

        for idx_val in df.index:
            assert isinstance(idx_val, str)
            assert idx_val in INCOME_STMT_MAP

    def test_columns_ordered_newest_first(self):
        from meridian_provider_sharadar.provider import INCOME_STMT_MAP, _build_df

        rows = [
            _make_sf1_row(date="2024-12-31"),
            _make_sf1_row(date="2023-12-31"),
            _make_sf1_row(date="2022-12-31"),
        ]
        df = _build_df(rows, INCOME_STMT_MAP)

        assert len(df.columns) == 3
        assert df.columns[0] == pd.Timestamp("2024-12-31")
        assert df.columns[1] == pd.Timestamp("2023-12-31")
        assert df.columns[2] == pd.Timestamp("2022-12-31")

    def test_values_match_sf1_data(self):
        from meridian_provider_sharadar.provider import INCOME_STMT_MAP, _build_df

        rows = [_make_sf1_row(revenue=9_999_999)]
        df = _build_df(rows, INCOME_STMT_MAP)

        col = df.columns[0]
        assert df.loc["Total Revenue", col] == 9_999_999

    def test_multiple_years_columns(self):
        from meridian_provider_sharadar.provider import BALANCE_SHEET_MAP, _build_df

        rows = [
            _make_sf1_row(date="2024-12-31"),
            _make_sf1_row(date="2023-12-31"),
            _make_sf1_row(date="2022-12-31"),
        ]
        df = _build_df(rows, BALANCE_SHEET_MAP)

        assert len(df.columns) == 3

    def test_empty_rows_returns_empty_df(self):
        from meridian_provider_sharadar.provider import INCOME_STMT_MAP, _build_df

        df = _build_df([], INCOME_STMT_MAP)
        assert df.empty


class TestCacheTTL:
    """Tests for cache expiry and freshness via _read_cache / _write_cache."""

    def test_expired_sf1_cache_returns_none(self, tmp_path):
        from datetime import timedelta

        from meridian_provider_sharadar.provider import _read_cache

        key = "TEST_sf1"
        identity = _cache_identity(table="SF1", ticker="TEST", dimension="MRY")
        old_time = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
        _write_cache(
            str(tmp_path),
            key,
            {"rows": [{"test": 1}]},
            identity=identity,
            source_fetched_at=old_time,
        )

        result = _read_cache(str(tmp_path), key, ttl_hours=168, identity=identity)
        assert result is None

    def test_fresh_sf1_cache_returns_data(self, tmp_path):
        from datetime import timedelta

        from meridian_provider_sharadar.provider import _read_cache

        key = "TEST_sf1"
        identity = _cache_identity(table="SF1", ticker="TEST", dimension="MRY")
        fresh_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_cache(
            str(tmp_path),
            key,
            {"rows": [{"test": 1}]},
            identity=identity,
            source_fetched_at=fresh_time,
        )

        result = _read_cache(str(tmp_path), key, ttl_hours=168, identity=identity)
        assert result is not None
        assert result["payload"]["rows"] == [{"test": 1}]

    def test_expired_prices_cache_returns_none(self, tmp_path):
        from datetime import timedelta

        from meridian_provider_sharadar.provider import _read_cache

        key = "TEST_prices_sep"
        identity = _cache_identity(table="SEP", ticker="TEST", start="1990-01-01")
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        _write_cache(
            str(tmp_path),
            key,
            {"rows": []},
            identity=identity,
            source_fetched_at=old_time,
        )

        result = _read_cache(str(tmp_path), key, ttl_hours=24, identity=identity)
        assert result is None

    def test_fresh_prices_cache_returns_data(self, tmp_path):
        from datetime import timedelta

        from meridian_provider_sharadar.provider import _read_cache

        key = "TEST_prices_sep"
        identity = _cache_identity(table="SEP", ticker="TEST", start="1990-01-01")
        fresh_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_cache(
            str(tmp_path),
            key,
            {"rows": [{"price": 100}]},
            identity=identity,
            source_fetched_at=fresh_time,
        )

        result = _read_cache(str(tmp_path), key, ttl_hours=24, identity=identity)
        assert result is not None
        assert result["payload"]["rows"] == [{"price": 100}]

    def test_cache_corrupted_file_returns_none(self, tmp_path):
        from meridian_provider_sharadar.provider import _read_cache

        key = "CORRUPT_sf1"
        path = _cache_path(str(tmp_path), key)
        path.write_text("NOT VALID JSON {{{")

        result = _read_cache(
            str(tmp_path),
            key,
            ttl_hours=168,
            identity=_cache_identity(table="SF1", ticker="CORRUPT", dimension="MRY"),
        )
        assert result is None


class TestCacheIdentityAndIntegrity:
    def test_cache_rejects_wrong_query_identity(self, tmp_path):
        from meridian_provider_sharadar.provider import _read_cache

        key = "AAPL_sf1"
        identity = _cache_identity(table="SF1", ticker="AAPL", dimension="MRY")
        _write_cache(str(tmp_path), key, {"rows": []}, identity=identity)

        assert (
            _read_cache(
                str(tmp_path),
                key,
                168,
                identity=_cache_identity(table="SF1", ticker="MSFT", dimension="MRY"),
            )
            is None
        )

    def test_cache_rejects_future_acquisition_time(self, tmp_path):
        from datetime import timedelta

        from meridian_provider_sharadar.provider import _read_cache

        key = "AAPL_sf1"
        identity = _cache_identity(table="SF1", ticker="AAPL", dimension="MRY")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        _write_cache(
            str(tmp_path),
            key,
            {"rows": []},
            identity=identity,
            source_fetched_at=future,
        )

        assert _read_cache(str(tmp_path), key, 168, identity=identity) is None

    def test_cache_rejects_content_hash_mismatch(self, tmp_path):
        from meridian_provider_sharadar.provider import _read_cache

        key = "AAPL_sf1"
        identity = _cache_identity(table="SF1", ticker="AAPL", dimension="MRY")
        _write_cache(str(tmp_path), key, {"rows": []}, identity=identity)
        path = _cache_path(str(tmp_path), key)
        envelope = json.loads(path.read_text())
        envelope["payload"]["rows"] = [{"ticker": "MSFT"}]
        path.write_text(json.dumps(envelope))

        assert _read_cache(str(tmp_path), key, 168, identity=identity) is None

    def test_raw_acquisition_provenance_survives_cache_read(self, tmp_path):
        _write_sf1_cache(
            tmp_path,
            "AAPL",
            [{"calendardate": "2024-12-31", "revenue": 100.0}],
        )
        provider = _make_provider(tmp_path)

        provider.get_financials("AAPL")
        observations = provider.provenance_for("AAPL")

        assert len(observations) == 1
        assert observations[0]["identity"] == _cache_identity(table="SF1", ticker="AAPL", dimension="MRY")
        assert observations[0]["source"] == "SHARADAR/SF1"
        assert len(observations[0]["sha256"]) == 64
        assert datetime.fromisoformat(observations[0]["source_fetched_at"]).tzinfo is not None

    def test_provenance_capture_isolated_between_concurrent_derived_reads(self, tmp_path):
        provider = _make_provider(tmp_path)
        barrier = threading.Barrier(2)
        captured = {}

        def worker(ticker, start):
            with provider.capture_provenance() as observations:
                barrier.wait()
                provider._record_observation(
                    ticker,
                    {
                        "identity": _cache_identity(table="SEP", ticker=ticker, start=start),
                        "source_fetched_at": datetime.now(timezone.utc).isoformat(),
                        "sha256": ("a" if ticker == "AAPL" else "b") * 64,
                    },
                )
            captured[ticker] = [entry["identity"] for entry in observations.values()]

        first = threading.Thread(target=worker, args=("AAPL", "2020-01-01"))
        second = threading.Thread(target=worker, args=("MSFT", "2019-01-01"))
        first.start()
        second.start()
        first.join()
        second.join()

        assert captured == {
            "AAPL": [_cache_identity(table="SEP", ticker="AAPL", start="2020-01-01")],
            "MSFT": [_cache_identity(table="SEP", ticker="MSFT", start="2019-01-01")],
        }


class TestSharadarBeta:
    @staticmethod
    def _price_frame(ticker, returns):
        end = pd.Timestamp.now(tz="UTC").tz_localize(None).to_period("M") - 1
        periods = pd.period_range(end=end, periods=len(returns) + 1, freq="M")
        prices = [100.0]
        for monthly_return in returns:
            prices.append(prices[-1] * (1 + monthly_return))
        return pd.DataFrame(
            {
                "ticker": ticker,
                "date": periods.to_timestamp("M"),
                "closeadj": prices,
            }
        )

    def test_beta_uses_only_aligned_sharadar_total_returns(self, tmp_path):
        provider = SharadarProvider(api_key="test-key", cache_dir=str(tmp_path))
        benchmark_returns = [0.01, -0.02, 0.03, 0.005, -0.01] * 6
        target_returns = [1.5 * value for value in benchmark_returns]
        frames = {
            ("AAPL", "SEP"): self._price_frame("AAPL", target_returns),
            ("SPY", "SFP"): self._price_frame("SPY", benchmark_returns),
        }
        provider._fetch_prices = MagicMock(side_effect=lambda ticker, table, start: frames[(ticker, table)])

        assert provider.get_beta("AAPL") == pytest.approx(1.5)
        assert {(call.args[0], call.args[1]) for call in provider._fetch_prices.call_args_list} == {
            ("AAPL", "SEP"),
            ("SPY", "SFP"),
        }

    def test_failed_beta_discards_partial_target_provenance(self, tmp_path):
        provider = SharadarProvider(api_key="test-key", cache_dir=str(tmp_path))
        target_identity = _cache_identity(table="SEP", ticker="AAPL", start="2021-06-01")

        def prices(ticker, table, start):
            if ticker == "SPY":
                raise ValueError("synthetic benchmark outage")
            provider._record_observation(
                ticker,
                {
                    "identity": target_identity,
                    "source_fetched_at": datetime.now(timezone.utc).isoformat(),
                    "sha256": "a" * 64,
                },
            )
            return self._price_frame(ticker, [0.01] * 30)

        provider._fetch_prices = MagicMock(side_effect=prices)

        with provider.capture_provenance() as captured, pytest.raises(ValueError, match="synthetic benchmark outage"):
            provider.get_beta("AAPL")

        assert captured == {}
        assert provider.provenance_for("AAPL") == []

    @pytest.mark.parametrize("missing_date", [None, pd.NaT, "NaT"])
    def test_monthly_beta_history_rejects_missing_or_nat_dates(self, missing_date):
        frame = pd.DataFrame({"date": [missing_date], "closeadj": [100.0]})

        with pytest.raises(ValueError, match="price dates are missing"):
            SharadarProvider._monthly_adjusted_close(frame, label="AAPL")

    def test_beta_rejects_gapped_or_insufficient_months(self, tmp_path):
        provider = SharadarProvider(api_key="test-key", cache_dir=str(tmp_path))
        returns = [0.01, -0.02, 0.03] * 10
        target = self._price_frame("AAPL", returns).drop(index=5).reset_index(drop=True)
        benchmark = self._price_frame("SPY", returns)
        provider._fetch_prices = MagicMock(
            side_effect=lambda ticker, table, start: target if ticker == "AAPL" else benchmark
        )

        with pytest.raises(ValueError, match="gaps"):
            provider.get_beta("AAPL")
