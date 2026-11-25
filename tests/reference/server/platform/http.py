import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_cors_raw = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
_LOCAL_APP_ORIGINS = frozenset(
    {
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    }
)


def _is_loopback_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.path not in {"", "/"}:
            return False
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            return False
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_local_app_origin(origin: str) -> bool:
    return origin in _LOCAL_APP_ORIGINS or _is_loopback_origin(origin)


_invalid_cors_origins = [origin for origin in _cors_origins if not _is_local_app_origin(origin)]
if _invalid_cors_origins:
    raise RuntimeError(
        "CORS_ORIGINS must contain only loopback http(s) or exact Tauri app origins; Meridian's local API "
        f"is not a remotely deployable service: {_invalid_cors_origins!r}"
    )
_CORS_ORIGIN_SET = frozenset(_cors_origins)


class _LocalMutationOriginMiddleware(BaseHTTPMiddleware):
    """Reject browser cross-site requests before endpoint side effects."""

    _MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
    _SIDE_EFFECTING_GET_PATHS = frozenset({"/api/index-constituents", "/api/validate-ticker", "/api/plugins"})
    _SIDE_EFFECTING_GET_PREFIXES = ("/api/territories/", "/api/plugins/")
    _BOOKMARKLET_PATH = "/api/realestate-br/bookmarklet-ingest"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        has_side_effects = request.method in self._MUTATING_METHODS or (
            request.method == "GET"
            and (path in self._SIDE_EFFECTING_GET_PATHS or path.startswith(self._SIDE_EFFECTING_GET_PREFIXES))
        )
        if has_side_effects and path != self._BOOKMARKLET_PATH:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            untrusted_origin = origin is not None and origin not in _CORS_ORIGIN_SET
            originless_cross_site = origin is None and fetch_site == "cross-site"
            if untrusted_origin or originless_cross_site:
                return JSONResponse({"detail": "Cross-origin side-effecting requests are not allowed"}, status_code=403)
        return await call_next(request)
