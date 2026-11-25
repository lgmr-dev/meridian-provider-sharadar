"""Conformance of the plug-in process with `contracts/plugin-runtime/v1`."""

import json
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

import pytest
from support import ROOT, SYNTHETIC_KEY, Client, contract_validator, handshake, start_process, write_plugin_json

RUNTIME = "plugin-runtime/v1/openapi.json#/components/schemas/"


@pytest.fixture
def plugin(tmp_path):
    cache = tmp_path / "home" / "meridian-provider-sharadar" / "cache"
    cache.mkdir(parents=True)
    return {
        "plugin_json": write_plugin_json(tmp_path),
        "cache": cache,
        "lock": cache.parent / "plugin.lock",
        "processes": [],
    }


def start(plugin, api_key=SYNTHETIC_KEY, extra_env=None):
    line = handshake(plugin["cache"], plugin["lock"], api_key)
    process = start_process(plugin["plugin_json"], line, extra_env)
    plugin["processes"].append(process)
    ready = json.loads(process.stdout.readline())
    return process, line["token"], ready


@pytest.fixture(autouse=True)
def stop_children(plugin):
    yield
    for process in plugin["processes"]:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def finish(process) -> tuple[bytes, bytes]:
    process.stdin.close()
    process.wait(timeout=10)
    return process.stdout.read(), process.stderr.read()


def test_ready_line_manifest_and_health(plugin):
    process, token, ready = start(plugin)
    assert list(contract_validator(RUNTIME + "ReadyLine").iter_errors(ready)) == []
    client = Client(ready["port"], token)

    status, headers, manifest = client.request("GET", "/v1/manifest")
    assert status == 200
    assert list(contract_validator(RUNTIME + "Manifest").iter_errors(manifest)) == []
    plugin_json = json.loads(plugin["plugin_json"].read_text(encoding="utf-8"))
    for key in ("id", "family", "version", "contract_version", "contract_revision"):
        assert manifest[key] == plugin_json[key]
    assert manifest["capabilities"] == {
        "operations": ["prices", "statements", "info", "validate", "dividends", "validateCredentials"],
        "max_concurrency": 8,
    }
    status, _headers, health = client.request("GET", "/v1/health")
    assert (status, health) == (200, {"status": "ok", "tools": []})
    assert not any(name.lower().startswith("access-control") for name in headers)

    stdout, _stderr = finish(process)
    assert process.returncode == 0
    assert stdout == b""


def test_health_is_degraded_without_a_key(plugin):
    process, token, ready = start(plugin, api_key=None)
    client = Client(ready["port"], token)

    assert client.request("GET", "/v1/health")[2] == {"status": "degraded", "tools": []}
    status, payload = client.call("validate", {"ticker": "AAPL", "max_age_hours": 168})
    assert (status, payload["code"]) == (503, "credential_missing")
    finish(process)


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Authorization": "Bearer " + "0" * 64}, 401, "token_rejected"),
        ({"Authorization": ""}, 401, "token_rejected"),
        ({"Origin": "http://127.0.0.1:5173"}, 403, "origin_rejected"),
        ({"Host": "example.invalid"}, 421, "host_rejected"),
    ],
)
def test_requests_without_the_session_rules_are_rejected(plugin, headers, status, code):
    process, token, ready = start(plugin)
    client = Client(ready["port"], token)

    got, response_headers, payload = client.request("GET", "/v1/manifest", headers=headers)

    assert (got, payload["code"]) == (status, code)
    assert response_headers["Content-Type"] == "application/problem+json"
    assert list(contract_validator(RUNTIME + "Problem").iter_errors(payload)) == []
    finish(process)


def test_body_rules(plugin):
    process, token, ready = start(plugin)
    client = Client(ready["port"], token)

    assert (
        client.request("POST", "/v1/fund_fundamentals", {"ticker": "TEST11.SA"})[2]["code"] == "unsupported_operation"
    )
    assert client.request("GET", "/v1/other")[2]["code"] == "not_found"
    connection = HTTPConnection("127.0.0.1", ready["port"], timeout=10)
    body = b'{"ticker": "AAPL", "max_age_hours": NaN}'
    connection.request(
        "POST",
        "/v1/validate",
        body=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    assert (response.status, json.loads(response.read())["code"]) == (400, "invalid_json")
    connection.close()
    finish(process)


def test_an_error_answer_closes_the_connection(plugin):
    process, token, ready = start(plugin)
    connection = HTTPConnection("127.0.0.1", ready["port"], timeout=10)
    connection.request(
        "POST",
        "/v1/fund_fundamentals",
        body=b'{"ticker": "TEST11.SA"}',
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    response.read()

    assert (response.status, response.getheader("Connection")) == (404, "close")
    connection.close()
    finish(process)


def test_second_process_gets_lock_held_and_exits(plugin):
    first, _token, _ready = start(plugin)
    second, _token, ready = start(plugin)

    assert ready == {"error": "lock_held"}
    assert second.wait(timeout=10) == 0
    finish(first)


def test_exit_on_stdin_end_of_file_releases_the_lock(plugin):
    first, _token, _ready = start(plugin)
    finish(first)
    second, _token, ready = start(plugin)

    assert set(ready) == {"port"}
    finish(second)


def test_parallel_calls(plugin):
    process, token, ready = start(plugin, api_key=None)
    client = Client(ready["port"], token)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.request("GET", "/v1/health")[0], range(32)))

    assert results == [200] * 32
    finish(process)


def test_listens_only_on_loopback(plugin):
    process, _token, ready = start(plugin)
    with socket.create_connection(("127.0.0.1", ready["port"]), timeout=5):
        pass
    addresses = {address[4][0] for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    for address in addresses - {"127.0.0.1"}:
        with pytest.raises(OSError):
            socket.create_connection((address, ready["port"]), timeout=2).close()
    finish(process)


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b'{"protocol_version": 2}\n',
        b"",
        json.dumps({**handshake(Path("/tmp/c"), Path("/tmp/l")), "locale": "fr"}).encode() + b"\n",
        json.dumps({**handshake(Path("/tmp/c"), Path("/tmp/l")), "locale": ["en"]}).encode() + b"\n",
    ],
)
def test_bad_handshake_gives_no_ready_line(plugin, line):
    process = subprocess.Popen(
        [sys.executable, "-m", "meridian_provider_sharadar", str(plugin["plugin_json"])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
    )
    plugin["processes"].append(process)
    stdout, _stderr = process.communicate(line, timeout=20)

    assert process.returncode == 2
    assert stdout == b""


def test_key_is_not_in_the_process_output(plugin):
    process, token, ready = start(plugin)
    client = Client(ready["port"], token)

    client.request("GET", "/v1/manifest")
    client.call("validate", {"ticker": "lower", "max_age_hours": 168})
    stdout, stderr = finish(process)

    assert SYNTHETIC_KEY.encode() not in stdout + stderr


def test_unsupported_methods_get_a_json_problem(plugin):
    process, token, ready = start(plugin)
    client = Client(ready["port"], token)

    unauthenticated = client.request("OPTIONS", "/v1/manifest", headers={"Authorization": ""})
    authenticated = client.request("DELETE", "/v1/manifest")

    assert (unauthenticated[0], unauthenticated[2]["code"]) == (401, "token_rejected")
    assert (authenticated[0], authenticated[2]["code"]) == (405, "method_not_allowed")
    assert authenticated[1]["Content-Type"] == "application/problem+json"
    assert authenticated[1]["Allow"] == "GET"
    assert client.request("PUT", "/v1/validate")[1]["Allow"] == "POST"
    assert client.request("PUT", "/v1/other")[0] == 404
    wrong_get = client.request("GET", "/v1/validate")
    wrong_post = client.request("POST", "/v1/manifest", {})
    assert (wrong_get[0], wrong_get[1]["Allow"]) == (405, "POST")
    assert (wrong_post[0], wrong_post[1]["Allow"]) == (405, "GET")
    with socket.create_connection(("127.0.0.1", ready["port"]), timeout=10) as raw:
        request = f"HEAD /v1/manifest HTTP/1.1\r\nHost: 127.0.0.1:{ready['port']}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n"
        raw.sendall(request.encode("ascii"))
        received = b""
        while chunk := raw.recv(65536):
            received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 405") and body == b""
    finish(process)


def test_ambient_nasdaq_settings_are_ignored(plugin):
    key_file = plugin["plugin_json"].parent / ".nasdaq" / "data_link_apikey"
    key_file.parent.mkdir()
    key_file.write_text("", encoding="utf-8")
    environment = {"NASDAQ_DATA_LINK_API_KEY": "", "NASDAQ_DATA_LINK_BASE_DOMAIN": "example.invalid"}

    process, token, ready = start(plugin, extra_env=environment)

    assert Client(ready["port"], token).request("GET", "/v1/health")[2] == {"status": "ok", "tools": []}
    finish(process)
