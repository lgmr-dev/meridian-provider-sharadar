import json
import math
import re
import socket
import threading
import time
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit

from server.plugins.errors import PluginCrashed, PluginError, PluginProtocolError, PluginTimeout

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_JSON_TYPES = ("application/json", "application/problem+json")
_PROBLEM_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class PluginProblem(PluginError):
    status_code = 502

    def __init__(self, problem: dict, http_status: int) -> None:
        code = problem.get("code")
        if not isinstance(code, str) or _PROBLEM_CODE.fullmatch(code) is None:
            code = "plugin_problem"
        super().__init__(code, f"plug-in problem {problem.get('type')!r} with status {http_status}")
        self.http_status = http_status


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def loads_strict(data: bytes) -> object:
    return json.loads(data.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)


def dumps_strict(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    token: str


def request_json(
    endpoint: Endpoint,
    method: str,
    path: str,
    body: object | None,
    *,
    timeout: float,
    max_bytes: int = MAX_RESPONSE_BYTES,
    cancel: threading.Event | None = None,
) -> object:
    try:
        content = None if body is None else dumps_strict(body)
    except ValueError as exc:
        raise PluginError("invalid_request", "request body is not finite JSON", status_code=422) from exc
    headers = {"Authorization": f"Bearer {endpoint.token}", "Accept": "application/json"}
    if content is not None:
        headers["Content-Type"] = "application/json"
    if cancel is not None and cancel.is_set():
        raise PluginError("request_cancelled", "caller abandoned the request", status_code=499)
    location = urlsplit(endpoint.base_url)
    connection = HTTPConnection(location.hostname, location.port, timeout=timeout)
    finished = threading.Event()
    timed_out = threading.Event()
    deadline = time.monotonic() + timeout
    try:
        connection.connect()
    except TimeoutError as exc:
        connection.close()
        raise PluginTimeout("plug-in request timed out") from exc
    except OSError as exc:
        connection.close()
        raise PluginCrashed("plug-in connection failed") from exc
    sock = connection.sock

    def close_on_cancel_or_deadline() -> None:
        while not finished.wait(0.05):
            expired = time.monotonic() >= deadline
            if expired or (cancel is not None and cancel.is_set()):
                if expired:
                    timed_out.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    threading.Thread(target=close_on_cancel_or_deadline, daemon=True).start()
    try:
        connection.request(method, path, body=content, headers=headers)
        response = connection.getresponse()
        status = response.status
        if 300 <= status < 400:
            raise PluginProtocolError("redirect_rejected", "plug-in answered with a redirect")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise PluginProtocolError("response_too_large", "plug-in response exceeds the limit")
        media_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
    except TimeoutError as exc:
        raise PluginTimeout("plug-in request timed out") from exc
    except (OSError, HTTPException) as exc:
        if timed_out.is_set():
            raise PluginTimeout("plug-in request timed out") from exc
        if cancel is not None and cancel.is_set():
            raise PluginError("request_cancelled", "caller abandoned the request", status_code=499) from exc
        raise PluginCrashed("plug-in connection failed") from exc
    finally:
        finished.set()
        connection.close()
    if timed_out.is_set():
        raise PluginTimeout("plug-in request timed out")
    if cancel is not None and cancel.is_set():
        raise PluginError("request_cancelled", "caller abandoned the request", status_code=499)
    if status == 401:
        try:
            problem = loads_strict(data) if media_type in _JSON_TYPES else {}
        except (UnicodeDecodeError, ValueError, RecursionError):
            problem = {}
        raise PluginProblem(problem if isinstance(problem, dict) else {}, status)
    if media_type not in _JSON_TYPES:
        raise PluginProtocolError("invalid_content_type", "plug-in response is not JSON")
    try:
        payload = loads_strict(data)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PluginProtocolError("invalid_json", "plug-in response is not finite JSON") from exc
    if status >= 400:
        if not isinstance(payload, dict):
            raise PluginProtocolError("invalid_problem", "plug-in error is not an RFC 9457 object")
        raise PluginProblem(payload, status)
    if status != 200:
        raise PluginProtocolError("unexpected_status", f"plug-in answered with status {status}")
    return payload
