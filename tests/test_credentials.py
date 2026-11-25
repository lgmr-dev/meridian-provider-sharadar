"""Parity of ``validateCredentials`` with the key check of Meridian ``save_sharadar_key`` at aa5d0cb."""

import asyncio
import importlib
import json

import httpx
import pytest
import requests
from fastapi import HTTPException
from support import contract_validator, running_plugin

from meridian_provider_sharadar import credentials

CANDIDATE = "synthetic-candidate-key-42"
MARKET = "market-data/v1/openapi.json#/components/schemas/"
CASES = {
    "valid": (200, {"datatable": {"data": [["AAPL"]]}}),
    "rejected": (400, {}),
    "expired": (403, {}),
    "no_data": (200, {"datatable": {"data": []}}),
    "no_datatable": (200, {"meta": {}}),
    "malformed_data": (200, {"datatable": {"data": "malformed"}}),
    "empty_text_data": (200, {"datatable": {"data": ""}}),
    "null_data": (200, {"datatable": {"data": None}}),
    "server_error": (500, {}),
    "transport": (None, None),
}
EXPECTED = {
    "valid": ((200, None), (True, None)),
    "rejected": ((401, "Sharadar rejected the API key"), (False, "key_rejected")),
    "expired": ((401, "Invalid or expired API key"), (False, "key_invalid_or_expired")),
    "no_data": ((401, "API key valid but no data returned — check your subscription"), (False, "no_subscription_data")),
    "no_datatable": (
        (401, "API key valid but no data returned — check your subscription"),
        (False, "no_subscription_data"),
    ),
    "malformed_data": ((200, None), "source_unavailable"),
    "empty_text_data": ((401, "API key valid but no data returned — check your subscription"), "source_unavailable"),
    "null_data": ((401, "API key valid but no data returned — check your subscription"), "source_unavailable"),
    "server_error": ((502, "Sharadar API returned HTTP 500"), "source_unavailable"),
    "transport": ((502, "Could not reach Sharadar API"), "source_unavailable"),
}


@pytest.fixture
def reference_route(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    route = importlib.import_module("server.platform.sharadar_key")
    monkeypatch.setattr(route.runtime._state, "data_dir", str(tmp_path / "data"))
    return route


def reference_result(route, monkeypatch, case: str):
    status, body = CASES[case]

    async def get(self, url, params=None, **kwargs):
        if status is None:
            raise httpx.ConnectError("synthetic transport failure")
        return httpx.Response(status, json=body, request=httpx.Request("GET", url, params=params))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    try:
        asyncio.run(route.save_sharadar_key(route.SharadarKeyRequest(api_key=CANDIDATE)))
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return 200, None


def plugin_result(tmp_path, monkeypatch, case: str):
    status, body = CASES[case]
    sent = []

    def get(url, **kwargs):
        sent.append((url, kwargs))
        if status is None:
            raise requests.ConnectionError("synthetic transport failure")
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode("utf-8")
        return response

    monkeypatch.setattr(credentials.requests, "get", get)
    with running_plugin(tmp_path / "plugin", api_key=None) as client:
        result = client.call("validateCredentials", {"secret_name": "api_key", "candidate": f" {CANDIDATE} "})
    assert sent == [
        (
            credentials.TICKERS_URL,
            {
                "params": {"ticker": "AAPL"},
                "headers": {"X-Api-Token": CANDIDATE, "Accept": "application/json"},
                "timeout": credentials.TIMEOUT_SECONDS,
                "allow_redirects": False,
            },
        )
    ]
    return result


@pytest.mark.parametrize("case", sorted(CASES))
def test_validate_credentials_matches_the_in_tree_key_check(reference_route, tmp_path, monkeypatch, case):
    in_tree, plugin = EXPECTED[case]

    assert reference_result(reference_route, monkeypatch, case) == in_tree
    status, payload = plugin_result(tmp_path, monkeypatch, case)

    if isinstance(plugin, str):
        assert (status, payload["code"], payload["type"]) == (503, plugin, f"urn:meridian:market-data:{plugin}")
        assert list(contract_validator(MARKET + "SourceProblem").iter_errors(payload)) == []
    else:
        assert status == 200
        assert (payload["valid"], payload["code"]) == plugin
        assert list(contract_validator(MARKET + "CredentialsResponse").iter_errors(payload)) == []
    assert CANDIDATE not in json.dumps(payload)


@pytest.mark.parametrize("candidate", ["abc\u200b123", "ключ", "abc\ndef"])
def test_a_candidate_that_cannot_be_a_header_value_is_rejected(tmp_path, monkeypatch, candidate):
    monkeypatch.setattr(credentials.requests, "get", lambda *args, **kwargs: pytest.fail("no request expected"))

    with running_plugin(tmp_path / "plugin", api_key=None) as client:
        status, payload = client.call("validateCredentials", {"secret_name": "api_key", "candidate": candidate})

    assert (status, payload["valid"], payload["code"]) == (200, False, "key_rejected")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"secret_name": "sharadar_api_key", "candidate": CANDIDATE}, "unknown_secret"),
        ({"secret_name": "API_KEY", "candidate": CANDIDATE}, "invalid_request"),
        ({"secret_name": "api_key", "candidate": "   "}, "invalid_request"),
        ({"secret_name": "api_key", "candidate": "x" * 4097}, "invalid_request"),
        ({"secret_name": "api_key"}, "invalid_request"),
    ],
)
def test_validate_credentials_rejects_bad_requests(tmp_path, monkeypatch, body, code):
    monkeypatch.setattr(credentials.requests, "get", lambda *args, **kwargs: pytest.fail("no request expected"))

    with running_plugin(tmp_path / "plugin", api_key=None) as client:
        status, payload = client.call("validateCredentials", body)

    assert (status, payload["code"]) == (422, code)
