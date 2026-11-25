"""Parity of the plug-in over HTTP with the Meridian in-tree provider at aa5d0cb, on synthetic tables.

The reference is ``tests/reference/server/sharadar_provider.py``. Each test feeds both the same
offline Nasdaq Data Link tables. The decoders here do what the Meridian host adapter must do:
parse each decimal string to a float, map ``null`` to a missing value and restore each observation.
"""

import json
import math
import shutil
import socket
from decimal import Decimal
from pathlib import Path

import nasdaqdatalink
import numpy as np
import pandas as pd
import pytest
from server.sharadar_provider import SharadarProvider as ReferenceProvider
from support import SYNTHETIC_KEY, FakeTables, contract_validator, running_plugin

from meridian_provider_sharadar import server
from meridian_provider_sharadar.provider import BALANCE_SHEET_MAP, CASHFLOW_MAP, INCOME_STMT_MAP

MARKET = "market-data/v1/openapi.json#/components/schemas/"
RESPONSE_SCHEMAS = {
    "prices": "PricesResponse",
    "statements": "TableResponse",
    "info": "InfoResponse",
    "validate": "ValidateResponse",
    "dividends": "TableResponse",
}
STATEMENTS = {
    "income": (INCOME_STMT_MAP, "get_financials"),
    "balance": (BALANCE_SHEET_MAP, "get_balance_sheet"),
    "cashflow": (CASHFLOW_MAP, "get_cashflow"),
}
CSV_COLUMNS = ["Open", "High", "Low", "Volume", "Close", "Adj Close"]
START = "2019-01-02"


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if not (isinstance(address, tuple) and address[0] == "127.0.0.1"):
            raise OSError("a test tried a network connection")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def tables(monkeypatch):
    fake = FakeTables()
    monkeypatch.setattr(nasdaqdatalink, "get_table", fake.get_table)
    return fake


@pytest.fixture
def paths(tmp_path):
    return {"reference": tmp_path / "reference", "plugin": tmp_path / "plugin", "csv": tmp_path / "csv"}


def reference(paths, ttl: int = 168) -> ReferenceProvider:
    return ReferenceProvider(api_key=SYNTHETIC_KEY, cache_dir=str(paths["reference"]), cache_ttl_hours=ttl)


def checked(operation: str, status: int, payload: dict) -> dict:
    assert status == 200, payload
    errors = list(contract_validator(MARKET + RESPONSE_SCHEMAS[operation]).iter_errors(payload))
    assert errors == []
    return payload


def assert_cell(cell: object, expected: object) -> None:
    if expected is None or (isinstance(expected, float) and math.isnan(expected)):
        assert cell is None
    elif isinstance(expected, int | np.integer) and not isinstance(expected, bool):
        assert cell == str(int(expected))
    else:
        assert isinstance(cell, str)
        assert Decimal(cell) == Decimal(repr(float(expected)))
        assert float(cell) == float(expected)


def decode_observation(item: dict) -> dict:
    assert item["source_fetched_at"].endswith("Z")
    return {
        "source": item["source"],
        "identity": json.loads(item["identity"]),
        "source_fetched_at": item["source_fetched_at"].removesuffix("Z") + "+00:00",
        "sha256": item["sha256"],
    }


def captured(provider: ReferenceProvider, call):
    with provider.capture_provenance() as capture:
        result = call()
    return result, [capture[key] for key in sorted(capture)]


def assert_observations(payload: dict, expected: list[dict], *, same_time: bool) -> None:
    decoded = [decode_observation(item) for item in payload["observations"]]
    if not same_time:
        decoded = [{**item, "source_fetched_at": None} for item in decoded]
        expected = [{**item, "source_fetched_at": None} for item in expected]
    assert decoded == expected


def decode_info(info: dict, fields: dict) -> dict:
    decoded = {name: value for name, value in info.items() if value is not None and name != "is_etf"}
    for name in ("marketCap", "beta"):
        if name in decoded:
            assert_cell(decoded[name], fields[name])
            decoded[name] = float(decoded[name])
    return decoded


def decode_number_column(cells: list) -> np.ndarray:
    """Parse with Python ``float()``, which rounds to the nearest double. ``pandas.to_numeric`` does not."""
    if any(cell is None or "." in cell for cell in cells):
        return np.array([math.nan if cell is None else float(cell) for cell in cells], dtype=float)
    return np.array([int(cell) for cell in cells], dtype=np.int64)


def decode_price_frame(table: dict) -> pd.DataFrame:
    names = [column["name"] for column in table["columns"]]
    rows = table["rows"]
    frame = pd.DataFrame(
        {name: decode_number_column([row[i] for row in rows]) for i, name in enumerate(names[1:], 1)},
        index=pd.DatetimeIndex(pd.to_datetime([row[0] for row in rows]), name="Date"),
    )
    return frame[CSV_COLUMNS]


@pytest.mark.parametrize("kind", sorted(STATEMENTS))
@pytest.mark.parametrize("shared_cache", [False, True])
def test_statements_match_the_in_tree_frames(tables, paths, kind, shared_cache):
    mapping, method = STATEMENTS[kind]
    provider = reference(paths)
    frame, expected = captured(provider, lambda: getattr(provider, method)("AAPL"))
    if shared_cache:
        shutil.copytree(paths["reference"], paths["plugin"])
    calls = len(tables.calls)

    with running_plugin(paths["plugin"]) as client:
        payload = checked(
            "statements", *client.call("statements", {"ticker": "AAPL", "kind": kind, "max_age_hours": 168})
        )

    table = payload["table"]
    assert [column["name"] for column in table["columns"]] == ["period_end", *mapping]
    assert [row[0] for row in table["rows"]] == [period.date().isoformat() for period in frame.columns]
    for row, period in zip(table["rows"], frame.columns):
        for label, cell in zip(mapping, row[1:]):
            assert_cell(cell, frame.at[label, period])
    assert_observations(payload, expected, same_time=shared_cache)
    assert (len(tables.calls) == calls) is shared_cache


def test_decimal_edges_use_the_plain_expansion(tables, paths):
    with running_plugin(paths["plugin"]) as client:
        payload = checked(
            "statements", *client.call("statements", {"ticker": "AAPL", "kind": "balance", "max_age_hours": 168})
        )

    labels = [column["name"] for column in payload["table"]["columns"]]
    newest = payload["table"]["rows"][0]
    assert newest[labels.index("Total Assets")] == "20000000000000000"
    assert newest[labels.index("Other Short Term Investments")] == "0.00001"


def test_dividends_match_the_in_tree_series(tables, paths):
    provider = reference(paths)
    series, expected = captured(provider, lambda: provider.get_dividends("AAPL"))

    with running_plugin(paths["plugin"]) as client:
        payload = checked("dividends", *client.call("dividends", {"ticker": "AAPL", "max_age_hours": 168}))

    rows = payload["table"]["rows"]
    assert [row[0] for row in rows] == [period.date().isoformat() for period in series.index]
    for row, value in zip(rows, series.tolist()):
        assert_cell(row[1], value)
    assert any(row[1] is None for row in rows)
    assert_observations(payload, expected, same_time=False)


@pytest.mark.parametrize("ticker", ["AAPL", "SPY"])
@pytest.mark.parametrize("beta", [False, True])
def test_info_matches_the_in_tree_dict(tables, paths, ticker, beta):
    provider = reference(paths)
    fields, expected = captured(provider, lambda: provider.get_ticker_info(ticker, include_beta=beta))
    is_etf = provider.is_etf(ticker)

    with running_plugin(paths["plugin"]) as client:
        payload = checked("info", *client.call("info", {"ticker": ticker, "beta": beta, "max_age_hours": 168}))

    assert payload["info"]["is_etf"] is is_etf
    assert decode_info(payload["info"], fields) == fields
    assert ("beta" in fields) is (beta and ticker != "SPY")
    assert_observations(payload, expected, same_time=False)


def test_info_keeps_partial_results_when_beta_fails(tables, paths):
    tables.empty.add("SFP")
    provider = reference(paths)
    fields = provider.get_ticker_info("AAPL", include_beta=True)

    with running_plugin(paths["plugin"]) as client:
        payload = checked("info", *client.call("info", {"ticker": "AAPL", "beta": True, "max_age_hours": 168}))

    assert "beta" not in fields
    assert payload["info"]["beta"] is None
    assert payload["info"]["shortName"] == fields["shortName"]


@pytest.mark.parametrize("delisted", ["N", "Y"])
def test_validate_matches_the_in_tree_result(tables, paths, delisted):
    tables.tickers["AAPL"] = {"isdelisted": delisted}
    expected = reference(paths).validate_ticker("AAPL")

    with running_plugin(paths["plugin"]) as client:
        payload = checked("validate", *client.call("validate", {"ticker": "AAPL", "max_age_hours": 168}))

    assert {"valid": payload["valid"], "name": payload["name"]} == expected


@pytest.mark.parametrize("ticker", ["AAPL", "SPY"])
@pytest.mark.parametrize("force_refresh", [False, True])
def test_prices_rebuild_the_in_tree_csv_byte_for_byte(tables, paths, capsys, ticker, force_refresh):
    provider = reference(paths)
    currency = provider.get_price_currency(ticker)
    path = provider.download_prices_csv(ticker, str(paths["csv"]), START, force_refresh=True)
    table_name = "SFP" if provider.is_etf(ticker) else "SEP"
    raw = provider._fetch_prices(ticker, table_name, START)

    with running_plugin(paths["plugin"]) as client:
        payload = checked(
            "prices",
            *client.call(
                "prices", {"ticker": ticker, "start": START, "force_refresh": force_refresh, "max_age_hours": 168}
            ),
        )

    assert payload["price_currency"] == currency
    frame = decode_price_frame(payload["table"])
    rebuilt = paths["csv"] / "rebuilt.csv"
    frame.to_csv(rebuilt)
    assert rebuilt.read_bytes() == Path(path).read_bytes()
    assert np.array_equal(frame["Close"].to_numpy(), pd.to_numeric(raw["close"]).to_numpy())
    assert np.array_equal(frame["Adj Close"].to_numpy(), pd.to_numeric(raw["closeadj"]).to_numpy())
    assert payload["table"]["rows"][0][1] == "0.00001"
    assert f"SHARADAR/{table_name}" in [item["source"] for item in payload["observations"]]
    assert json.loads(payload["provenance"]["identity"])["start"] == START


def test_prices_shared_cache_gives_the_in_tree_observations(tables, paths, capsys):
    provider = reference(paths)

    def engine_path():
        provider.get_price_currency("AAPL")
        provider.download_prices_csv("AAPL", str(paths["csv"]), START)

    _result, expected = captured(provider, engine_path)
    shutil.copytree(paths["reference"], paths["plugin"])
    calls = len(tables.calls)

    with running_plugin(paths["plugin"]) as client:
        payload = checked(
            "prices",
            *client.call("prices", {"ticker": "AAPL", "start": START, "force_refresh": False, "max_age_hours": 168}),
        )

    assert len(tables.calls) == calls
    assert_observations(payload, expected, same_time=True)
    assert payload["provenance"]["source_fetched_at"] is not None


def problem(status: int, payload: dict) -> tuple[int, str, str]:
    errors = list(contract_validator(MARKET + "SourceProblem").iter_errors(payload))
    assert errors == []
    return status, payload["type"], payload["code"]


UNAVAILABLE = (503, "urn:meridian:market-data:source_unavailable", "source_unavailable")
INVALID = (502, "urn:meridian:market-data:source_invalid", "source_invalid")


@pytest.mark.parametrize("kind", sorted(STATEMENTS))
def test_sf1_failure_is_unavailable_where_the_in_tree_result_is_empty(tables, paths, kind):
    tables.fail.add("SF1")
    mapping, method = STATEMENTS[kind]
    assert getattr(reference(paths), method)("AAPL").empty
    assert reference(paths).get_dividends("AAPL").empty

    with running_plugin(paths["plugin"]) as client:
        statements = client.call("statements", {"ticker": "AAPL", "kind": kind, "max_age_hours": 168})
        dividends = client.call("dividends", {"ticker": "AAPL", "max_age_hours": 168})

    assert problem(*statements) == UNAVAILABLE
    assert problem(*dividends) == UNAVAILABLE


def test_sf1_zero_rows_give_a_zero_row_table(tables, paths):
    tables.empty.add("SF1")
    assert reference(paths).get_financials("AAPL").empty

    with running_plugin(paths["plugin"]) as client:
        payload = checked(
            "statements", *client.call("statements", {"ticker": "AAPL", "kind": "income", "max_age_hours": 168})
        )

    assert payload["table"]["rows"] == []
    assert payload["provenance"]["source_fetched_at"] is None


@pytest.mark.parametrize(("failure", "expected"), [("fail", UNAVAILABLE), ("empty", None)])
def test_tickers_failure_and_zero_rows_for_validate(tables, paths, failure, expected):
    getattr(tables, failure).add("TICKERS")
    assert reference(paths).validate_ticker("AAPL") == {"valid": False, "name": None}

    with running_plugin(paths["plugin"]) as client:
        status, payload = client.call("validate", {"ticker": "AAPL", "max_age_hours": 168})

    if expected is None:
        assert (status, payload["valid"], payload["name"]) == (200, False, None)
    else:
        assert problem(status, payload) == expected


@pytest.mark.parametrize(("failure", "expected"), [("fail", UNAVAILABLE), ("empty", INVALID)])
def test_tickers_failure_and_zero_rows_fail_prices(tables, paths, failure, expected):
    getattr(tables, failure).add("TICKERS")
    with pytest.raises(ValueError, match="price currency"):
        reference(paths).get_price_currency("AAPL")

    with running_plugin(paths["plugin"]) as client:
        result = client.call("prices", {"ticker": "AAPL", "start": START, "force_refresh": True, "max_age_hours": 168})

    assert problem(*result) == expected


def test_tickers_failure_keeps_the_info_fallback_fields(tables, paths):
    tables.fail.add("TICKERS")
    fields = reference(paths).get_ticker_info("AAPL")

    with running_plugin(paths["plugin"]) as client:
        payload = checked("info", *client.call("info", {"ticker": "AAPL", "beta": False, "max_age_hours": 168}))

    assert payload["info"]["is_etf"] is False
    assert decode_info(payload["info"], fields) == fields


@pytest.mark.parametrize(("failure", "expected"), [("fail", UNAVAILABLE), ("empty", None)])
def test_price_failure_and_zero_rows(tables, paths, capsys, failure, expected):
    getattr(tables, failure).add("SEP")
    with pytest.raises(ValueError, match="no price data"):
        reference(paths).download_prices_csv("AAPL", str(paths["csv"]), START)

    with running_plugin(paths["plugin"]) as client:
        status, payload = client.call(
            "prices", {"ticker": "AAPL", "start": START, "force_refresh": True, "max_age_hours": 168}
        )

    if expected is None:
        assert status == 200 and payload["table"]["rows"] == [] and payload["price_currency"] == "USD"
    else:
        assert problem(status, payload) == expected


def test_metadata_conflict_is_invalid(tables, paths):
    tables.tickers["AAPL"] = {"category": "ETF", "table": "SEP"}
    with pytest.raises(ValueError, match="conflicting"):
        reference(paths).validate_ticker("AAPL")

    with running_plugin(paths["plugin"]) as client:
        for operation, body in [
            ("validate", {"ticker": "AAPL", "max_age_hours": 168}),
            ("info", {"ticker": "AAPL", "beta": False, "max_age_hours": 168}),
            ("prices", {"ticker": "AAPL", "start": None, "force_refresh": False, "max_age_hours": 168}),
        ]:
            assert problem(*client.call(operation, body)) == INVALID


@pytest.mark.parametrize(("max_age", "refetch"), [(24, True), (168, False)])
def test_max_age_hours_bounds_the_raw_cache(tables, paths, max_age, refetch):
    with running_plugin(paths["plugin"]) as client:
        checked("statements", *client.call("statements", {"ticker": "AAPL", "kind": "income", "max_age_hours": 168}))
        envelope_path = paths["plugin"] / "cache" / "sharadar" / "AAPL_SF1.json"
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        old = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=30)
        envelope["source_fetched_at"] = old.isoformat()
        envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
        calls = len(tables.calls)
        payload = checked(
            "statements", *client.call("statements", {"ticker": "AAPL", "kind": "income", "max_age_hours": max_age})
        )

    assert (len(tables.calls) > calls) is refetch
    fetched = pd.Timestamp(payload["provenance"]["source_fetched_at"])
    assert (fetched > old) is refetch


def test_price_metadata_uses_max_age_hours(tables, paths):
    body = {"ticker": "AAPL", "start": START, "force_refresh": False, "max_age_hours": 24}
    with running_plugin(paths["plugin"]) as client:
        checked("prices", *client.call("prices", body))
        envelope_path = paths["plugin"] / "cache" / "sharadar" / "AAPL_META.json"
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        envelope["source_fetched_at"] = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=30)).isoformat()
        envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
        tables.calls.clear()
        checked("prices", *client.call("prices", body))

    assert [table for table, _ticker, _query in tables.calls] == ["SHARADAR/TICKERS"]


def test_request_errors(tables, paths):
    with running_plugin(paths["plugin"]) as client:
        assert client.call("validate", {"ticker": "PETR4.SA", "max_age_hours": 168})[1]["code"] == "invalid_ticker"
        assert client.call("validate", {"ticker": "aapl", "max_age_hours": 168})[1]["code"] == "invalid_request"
        assert client.call("validate", {"ticker": "AAPL", "max_age_hours": 0})[1]["code"] == "invalid_request"
        assert client.call("validate", {"ticker": "AAPL", "max_age_hours": True})[1]["code"] == "invalid_request"
        assert client.call("validate", {"ticker": "AAPL"})[1]["code"] == "invalid_request"
        assert (
            client.call("statements", {"ticker": "AAPL", "kind": "x", "max_age_hours": 1})[1]["code"]
            == "invalid_request"
        )
        start = {"ticker": "AAPL", "start": "2024-02-30", "force_refresh": False, "max_age_hours": 1}
        assert client.call("prices", start)[1]["code"] == "invalid_request"
    assert tables.calls == []


def test_no_api_key_gives_credential_missing(tables, paths):
    with running_plugin(paths["plugin"], api_key=None) as client:
        status, payload = client.call("validate", {"ticker": "AAPL", "max_age_hours": 168})

    assert (status, payload["code"]) == (503, "credential_missing")
    assert tables.calls == []


def test_key_is_not_in_logs_responses_or_cache_files(monkeypatch, paths, caplog):
    def leaking_transport(table, **query):
        raise ConnectionError(f"upstream refused {SYNTHETIC_KEY} for {table}")

    monkeypatch.setattr(nasdaqdatalink, "get_table", leaking_transport)
    caplog.set_level("DEBUG")
    bodies = [
        ("validate", {"ticker": "AAPL", "max_age_hours": 168}),
        ("info", {"ticker": "AAPL", "beta": True, "max_age_hours": 168}),
        ("statements", {"ticker": "AAPL", "kind": "income", "max_age_hours": 168}),
        ("dividends", {"ticker": "AAPL", "max_age_hours": 168}),
        ("prices", {"ticker": "AAPL", "start": None, "force_refresh": True, "max_age_hours": 168}),
    ]
    with running_plugin(paths["plugin"]) as client:
        responses = [client.call(operation, body) for operation, body in bodies]

    text = json.dumps(responses) + caplog.text
    text += "".join(path.read_text(errors="replace") for path in paths["plugin"].rglob("*") if path.is_file())
    assert SYNTHETIC_KEY not in text
    assert {status for status, _payload in responses} == {200, 503}


def test_the_nasdaq_endpoint_is_pinned(monkeypatch):
    monkeypatch.setattr(nasdaqdatalink.ApiConfig, "api_base", "https://example.invalid/api/v3")
    monkeypatch.setattr(nasdaqdatalink.ApiConfig, "verify_ssl", False)

    server.configure_nasdaq_client()

    assert nasdaqdatalink.ApiConfig.api_base == "https://data.nasdaq.com/api/v3"
    assert nasdaqdatalink.ApiConfig.verify_ssl is True


def test_sf1_rows_that_fail_a_check_are_invalid(tables, paths, monkeypatch):
    def rows_without_period(table, **query):
        frame = tables.get_table(table, **query)
        return frame.drop(columns=["reportperiod"]) if table == "SHARADAR/SF1" else frame

    monkeypatch.setattr(nasdaqdatalink, "get_table", rows_without_period)
    assert reference(paths).get_dividends("AAPL").empty

    with running_plugin(paths["plugin"]) as client:
        statements = client.call("statements", {"ticker": "AAPL", "kind": "income", "max_age_hours": 168})
        dividends = client.call("dividends", {"ticker": "AAPL", "max_age_hours": 168})

    assert problem(*statements) == INVALID
    assert problem(*dividends) == INVALID


def test_tickers_rows_that_fail_a_check_are_invalid(tables, paths, monkeypatch):
    def two_price_rows(table, **query):
        frame = tables.get_table(table, **query)
        return pd.concat([frame, frame.assign(table="SFP")]) if table == "SHARADAR/TICKERS" else frame

    monkeypatch.setattr(nasdaqdatalink, "get_table", two_price_rows)
    assert reference(paths).validate_ticker("AAPL") == {"valid": False, "name": None}

    with running_plugin(paths["plugin"]) as client:
        result = client.call("validate", {"ticker": "AAPL", "max_age_hours": 168})

    assert problem(*result) == INVALID


def test_prices_need_a_currency_where_the_in_tree_screening_history_did_not(tables, paths):
    tables.tickers["AAPL"] = {"currency": None}
    raw = reference(paths)._fetch_prices("AAPL", "SEP", START)
    assert not raw.empty

    with running_plugin(paths["plugin"]) as client:
        result = client.call("prices", {"ticker": "AAPL", "start": START, "force_refresh": False, "max_age_hours": 168})

    assert problem(*result) == INVALID


def test_the_client_follows_no_redirect(paths, monkeypatch):
    from nasdaqdatalink.connection import Connection

    original = Connection.__dict__["execute_request"]
    monkeypatch.setattr(Connection, "execute_request", original)
    monkeypatch.setattr(Connection, "_meridian_plugin_transport", False, raising=False)
    sent = []

    def request(self, method, url, **options):
        sent.append(options)
        response = __import__("requests").Response()
        response.status_code = 302
        response.headers["Location"] = "https://example.invalid/"
        response._content = b"{}"
        response.url = url
        return response

    monkeypatch.setattr("requests.Session.request", request)
    provider = __import__("meridian_provider_sharadar.provider", fromlist=["SharadarProvider"]).SharadarProvider(
        api_key=SYNTHETIC_KEY, cache_dir=str(paths["plugin"])
    )

    assert provider._fetch_sf1("AAPL") == []
    assert len(sent) == 1
    assert sent[0]["allow_redirects"] is False


def test_a_request_error_before_the_response_is_unavailable(tables, paths, monkeypatch):
    def invalid_header(table, **query):
        raise __import__("requests").exceptions.InvalidHeader("synthetic header failure")

    monkeypatch.setattr(nasdaqdatalink, "get_table", invalid_header)

    with running_plugin(paths["plugin"]) as client:
        statements = client.call("statements", {"ticker": "AAPL", "kind": "income", "max_age_hours": 168})
        validate = client.call("validate", {"ticker": "AAPL", "max_age_hours": 168})
        prices = client.call("prices", {"ticker": "SPY", "start": START, "force_refresh": True, "max_age_hours": 168})

    assert problem(*statements) == UNAVAILABLE
    assert problem(*validate) == UNAVAILABLE
    assert problem(*prices) == UNAVAILABLE


def test_a_tickers_row_with_a_wrong_field_type_is_invalid(tables, paths):
    tables.tickers["AAPL"] = {"category": 7}

    with running_plugin(paths["plugin"]) as client:
        result = client.call("validate", {"ticker": "AAPL", "max_age_hours": 168})

    assert problem(*result) == INVALID


def sdk_response(table: str, ticker: str) -> dict:
    if table == "TICKERS":
        row = FakeTables().ticker_row(ticker)
        columns = list(row)
        return {
            "datatable": {
                "data": [list(row.values())],
                "columns": [{"name": name, "type": "String"} for name in columns],
            },
            "meta": {"next_cursor_id": None},
        }
    return {
        "datatable": {
            "data": [[ticker, "2020-02-30", 10.0, 10.0]],
            "columns": [
                {"name": "ticker", "type": "String"},
                {"name": "date", "type": "Date"},
                {"name": "close", "type": "BigDecimal(15,3)"},
                {"name": "closeadj", "type": "BigDecimal(15,3)"},
            ],
        },
        "meta": {"next_cursor_id": None},
    }


@pytest.fixture
def real_sdk(monkeypatch):
    from nasdaqdatalink.connection import Connection

    monkeypatch.setattr(Connection, "execute_request", Connection.__dict__["execute_request"])
    monkeypatch.setattr(Connection, "_meridian_plugin_transport", False, raising=False)

    def request(self, method, url, **options):
        table = url.split("/SHARADAR/")[1].split(".")[0]
        response = __import__("requests").Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        body = bodies.get(table)
        response._content = (
            json.dumps(sdk_response(table, options["params"]["ticker"])).encode("utf-8") if body is None else body
        )
        response.url = url
        return response

    bodies: dict[str, bytes] = {}
    monkeypatch.setattr("requests.Session.request", request)
    return bodies


def test_a_row_that_the_client_cannot_parse_after_an_answer_is_invalid(paths, real_sdk):
    with running_plugin(paths["plugin"]) as client:
        result = client.call("prices", {"ticker": "AAPL", "start": START, "force_refresh": True, "max_age_hours": 168})

    assert problem(*result) == INVALID


def test_a_cache_directory_error_is_unavailable(tables, paths):
    paths["plugin"].mkdir()
    (paths["plugin"] / "cache").write_text("not a directory", encoding="utf-8")

    with running_plugin(paths["plugin"]) as client:
        result = client.call("statements", {"ticker": "AAPL", "kind": "income", "max_age_hours": 168})

    assert problem(*result) == UNAVAILABLE


@pytest.mark.parametrize(("body", "in_tree_raises"), [(b"not json", True), (b"{}", False)])
def test_an_answer_that_the_client_cannot_parse_is_invalid(paths, real_sdk, body, in_tree_raises):
    real_sdk["SEP"] = body
    provider = reference(paths)
    if in_tree_raises:
        with pytest.raises(ValueError):
            provider._fetch_prices("AAPL", "SEP", START)
    else:
        assert provider._fetch_prices("AAPL", "SEP", START).empty

    with running_plugin(paths["plugin"]) as client:
        result = client.call("prices", {"ticker": "AAPL", "start": START, "force_refresh": True, "max_age_hours": 168})

    assert problem(*result) == INVALID


def test_key_is_not_in_the_log_when_a_beta_query_fails(tables, paths, monkeypatch, caplog):
    def leaking_price_query(table, **query):
        if table in {"SHARADAR/SEP", "SHARADAR/SFP"}:
            raise ValueError(f"header value {SYNTHETIC_KEY!r} is not valid")
        return tables.get_table(table, **query)

    monkeypatch.setattr(nasdaqdatalink, "get_table", leaking_price_query)
    caplog.set_level("DEBUG")

    with running_plugin(paths["plugin"]) as client:
        status, payload = client.call("info", {"ticker": "AAPL", "beta": True, "max_age_hours": 168})

    assert status == 200 and payload["info"]["beta"] is None
    assert "Sharadar beta unavailable" in caplog.text
    assert SYNTHETIC_KEY not in caplog.text
