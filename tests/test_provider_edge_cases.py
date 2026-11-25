"""Edge-case tests for the Sharadar provider.

Verifies that:
1. _build_df handles degenerate inputs (empty, missing dates, None values).
2. get_dividends and _compute_dividend_adjusted_close handle NaN, zero
   shares, negative ncfdiv, and DPS-exceeds-price edge cases.
3. _build_df output ordering and duplicate-mapping semantics are correct.

All tests are pure unit tests — no network calls.
"""

import pandas as pd
import pytest

from meridian_provider_sharadar.provider import (
    BALANCE_SHEET_MAP,
    CASHFLOW_MAP,
    INCOME_STMT_MAP,
    SharadarProvider,
    _bare_ticker,
    _build_df,
    _cache_identity,
    _prices_cache_key,
    _write_cache,
)


def _make_provider(tmp_path, api_key="test-key"):
    """Create a SharadarProvider with test API key and temp cache dir."""
    return SharadarProvider(api_key=api_key, cache_dir=str(tmp_path))


def _write_sf1_cache(tmp_path, ticker, rows):
    """Pre-populate the SF1 cache for a ticker."""
    bare = _bare_ticker(ticker)
    _write_cache(
        str(tmp_path),
        f"{bare}_sf1",
        {
            "rows": [
                {
                    "ticker": bare,
                    "dimension": "MRY",
                    "reportperiod": row.get("reportperiod", row.get("calendardate")),
                    **row,
                }
                for row in rows
            ]
        },
        identity=_cache_identity(table="SF1", ticker=ticker, dimension="MRY"),
    )


def _write_meta_cache(tmp_path, ticker, meta):
    """Pre-populate the TICKERS metadata cache for a ticker."""
    bare = _bare_ticker(ticker)
    _write_cache(
        str(tmp_path),
        f"{bare}_meta",
        {"meta": {"ticker": bare, **meta}},
        identity=_cache_identity(table="TICKERS", ticker=ticker),
    )


def _write_prices_cache(tmp_path, ticker, table, rows):
    """Pre-populate the prices cache for a ticker."""
    key, start = _prices_cache_key(ticker, table, "1990-01-01")
    bare = _bare_ticker(ticker)
    _write_cache(
        str(tmp_path),
        key,
        {"rows": [{"ticker": bare, **row} for row in rows]},
        identity=_cache_identity(table=table, ticker=ticker, start=start),
    )


class TestEmptyData:
    """_build_df and provider methods handle empty / missing data gracefully."""

    def test_build_df_empty_rows(self):
        """_build_df with empty row list returns empty DataFrame."""
        result = _build_df([], INCOME_STMT_MAP)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_build_df_rows_missing_dates(self):
        """Rows without the fiscal report period are skipped entirely."""
        rows = [
            {"revenue": 1_000_000, "netinc": 100_000},
            {"foo": "bar"},
        ]
        result = _build_df(rows, INCOME_STMT_MAP)
        assert result.empty

    def test_get_dividends_empty_sf1(self, tmp_path):
        """No SF1 data cached for ticker returns empty Series."""
        _write_sf1_cache(tmp_path, "EMPTY", [])
        p = _make_provider(tmp_path)
        divs = p.get_dividends("EMPTY")
        assert isinstance(divs, pd.Series)
        assert len(divs) == 0


class TestNaNHandling:
    """NaN / None values in SF1 data are handled safely."""

    def test_latest_nan_ncfdiv_preserves_fiscal_period_and_fails_closed(self, tmp_path):
        _write_sf1_cache(
            tmp_path,
            "NANTEST",
            [
                {"calendardate": "2024-12-31", "ncfdiv": None, "shareswa": 100_000_000},
                {"calendardate": "2023-12-31", "ncfdiv": -50_000_000, "shareswa": 100_000_000},
            ],
        )
        p = _make_provider(tmp_path)
        divs = p.get_dividends("NANTEST")
        assert len(divs) == 2
        assert divs.iloc[0] == pytest.approx(0.5)
        assert pd.isna(divs.iloc[-1])

    def test_latest_nan_shareswa_preserves_fiscal_period_and_fails_closed(self, tmp_path):
        _write_sf1_cache(
            tmp_path,
            "NANSH",
            [
                {"calendardate": "2024-12-31", "ncfdiv": -50_000_000, "shareswa": None},
                {"calendardate": "2023-12-31", "ncfdiv": -50_000_000, "shareswa": 100_000_000},
            ],
        )
        p = _make_provider(tmp_path)
        divs = p.get_dividends("NANSH")
        assert len(divs) == 2
        assert divs.iloc[0] == pytest.approx(0.5)
        assert pd.isna(divs.iloc[-1])

    def test_build_df_none_values_become_none(self):
        """SF1 row with None for a mapped field produces None in the DataFrame."""
        rows = [
            {"reportperiod": "2024-12-31", "revenue": 1_000_000, "netinc": None},
        ]
        result = _build_df(rows, INCOME_STMT_MAP)
        assert not result.empty
        val = result.loc["Net Income"].iloc[0]
        assert val is None


class TestZeroShares:
    """Zero shares are handled safely in dividend calculations."""

    def test_latest_zero_shares_preserves_fiscal_period_and_fails_closed(self, tmp_path):
        _write_sf1_cache(
            tmp_path,
            "ZEROSH",
            [
                {"calendardate": "2024-12-31", "ncfdiv": -100_000_000, "shareswa": 0},
                {"calendardate": "2023-12-31", "ncfdiv": -100_000_000, "shareswa": 50_000_000},
            ],
        )
        p = _make_provider(tmp_path)
        divs = p.get_dividends("ZEROSH")
        assert len(divs) == 2
        assert divs.iloc[0] == pytest.approx(2.0)
        assert pd.isna(divs.iloc[-1])

    def test_zero_shares_does_not_make_annual_adjustment_identifiable(self, tmp_path):
        _write_sf1_cache(
            tmp_path,
            "ZEROADJ",
            [
                {"calendardate": "2024-06-30", "ncfdiv": -100_000_000, "shareswa": 0},
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
            p._compute_dividend_adjusted_close(prices, "ZEROADJ")


class TestNegativeNCFDiv:
    """ncfdiv sign convention and edge cases in dividend computation."""

    def test_negative_ncfdiv_abs_value(self, tmp_path):
        """ncfdiv=-200M, shareswa=100M produces DPS=2.0 (abs value used)."""
        _write_sf1_cache(
            tmp_path,
            "NEGDIV",
            [
                {"calendardate": "2024-12-31", "ncfdiv": -200_000_000, "shareswa": 100_000_000},
            ],
        )
        p = _make_provider(tmp_path)
        divs = p.get_dividends("NEGDIV")
        assert len(divs) == 1
        assert divs.iloc[0] == pytest.approx(2.0)

    def test_zero_ncfdiv_produces_zero_dps(self, tmp_path):
        """ncfdiv=0 produces DPS=0.0, and the row IS included in output."""
        _write_sf1_cache(
            tmp_path,
            "ZERODIV",
            [
                {"calendardate": "2024-12-31", "ncfdiv": 0, "shareswa": 100_000_000},
            ],
        )
        p = _make_provider(tmp_path)
        divs = p.get_dividends("ZERODIV")
        assert len(divs) == 1
        assert divs.iloc[0] == pytest.approx(0.0)

    def test_implausible_annual_dps_does_not_trigger_synthetic_adjustment(self, tmp_path):
        _write_sf1_cache(
            tmp_path,
            "BIGDIV",
            [
                {"calendardate": "2024-06-30", "ncfdiv": -200_000_000, "shareswa": 1_000_000},
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
            p._compute_dividend_adjusted_close(prices, "BIGDIV")


class TestBuildDfEdgeCases:
    """Edge cases in _build_df date handling, ordering, and duplicate mappings."""

    def test_dates_are_newest_first(self):
        """Output columns should be in input order (newest first, matching yfinance)."""
        rows = [
            {"reportperiod": "2024-12-31", "revenue": 200},
            {"reportperiod": "2023-12-31", "revenue": 100},
        ]
        result = _build_df(rows, INCOME_STMT_MAP)
        assert not result.empty
        cols = list(result.columns)
        assert cols[0] == pd.Timestamp("2024-12-31")
        assert cols[1] == pd.Timestamp("2023-12-31")

    def test_filing_and_calendar_dates_do_not_replace_reportperiod(self):
        """Filing and calendar dates cannot masquerade as fiscal periods."""
        rows = [
            {"datekey": "2024-12-31", "revenue": 200},
            {"calendardate": "2023-12-31", "revenue": 100},
        ]
        result = _build_df(rows, INCOME_STMT_MAP)
        assert result.empty

    def test_duplicate_sf1_columns_in_map(self):
        """Both "Cash And Cash Equivalents" and "Cash Cash Equivalents And
        Short Term Investments" map to the same Sharadar column (cashneq).

        When both appear in the balance sheet map, both rows in the output
        DataFrame should have the same value from the single cashneq field.
        """
        rows = [
            {"reportperiod": "2024-12-31", "cashneq": 500_000_000},
        ]
        result = _build_df(rows, BALANCE_SHEET_MAP)

        assert "Cash And Cash Equivalents" in result.index
        assert "Cash Cash Equivalents And Short Term Investments" in result.index

        val1 = result.loc["Cash And Cash Equivalents"].iloc[0]
        val2 = result.loc["Cash Cash Equivalents And Short Term Investments"].iloc[0]
        assert val1 == pytest.approx(500_000_000)
        assert val2 == pytest.approx(500_000_000)

    def test_build_df_single_row(self):
        """Single SF1 row produces a DataFrame with one column."""
        rows = [{"reportperiod": "2024-12-31", "revenue": 1_000_000, "netinc": 100_000}]
        result = _build_df(rows, INCOME_STMT_MAP)
        assert result.shape[1] == 1
        assert result.loc["Total Revenue"].iloc[0] == pytest.approx(1_000_000)
        assert result.loc["Net Income"].iloc[0] == pytest.approx(100_000)

    def test_build_df_unmapped_sf1_fields_ignored(self):
        """SF1 fields not in the mapping are silently ignored."""
        rows = [
            {
                "reportperiod": "2024-12-31",
                "revenue": 1_000_000,
                "some_random_field": 999,
                "another_field": "hello",
            },
        ]
        result = _build_df(rows, INCOME_STMT_MAP)
        assert "some_random_field" not in result.index
        assert "another_field" not in result.index
        assert "Total Revenue" in result.index

    def test_build_df_uses_fiscal_reportperiod_not_filing_date(self):
        """Fiscal alignment uses reportperiod, never the later filing date."""
        rows = [
            {
                "datekey": "2025-02-15",
                "calendardate": "2024-12-31",
                "reportperiod": "2024-12-31",
                "revenue": 1_000_000,
            },
        ]
        result = _build_df(rows, INCOME_STMT_MAP)
        assert result.shape[1] == 1
        assert result.columns[0] == pd.Timestamp("2024-12-31")

    def test_build_df_preserves_negative_values(self):
        """Negative values (e.g., capex, ncfdiv) are preserved as-is."""
        rows = [
            {
                "reportperiod": "2024-12-31",
                "capex": -90_000_000,
                "ncfdiv": -50_000_000,
                "ncfdebt": -10_000_000,
            },
        ]
        result = _build_df(rows, CASHFLOW_MAP)
        assert result.loc["Capital Expenditure"].iloc[0] == pytest.approx(-90_000_000)
        assert result.loc["Cash Dividends Paid"].iloc[0] == pytest.approx(-50_000_000)
        assert result.loc["Net Debt Issuance"].iloc[0] == pytest.approx(-10_000_000)
