import logging
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from server.platform import config_store, runtime

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_sharadar_key() -> str:
    """Resolve Sharadar API key: env var takes priority, then secrets file."""
    env_key = os.environ.get("SHARADAR_API_KEY", "").strip()
    if env_key:
        return env_key
    secrets_file = Path(runtime.get_data_dir()) / ".secrets.json"
    if secrets_file.exists():
        data = runtime._read_json_object(secrets_file, "Secrets file")
        key = data.get("sharadar_api_key", "")
        if not isinstance(key, str):
            raise RuntimeError("Secrets file sharadar_api_key must be a string")
        return key.strip()
    return ""


def _require_sharadar_key(data_source: str) -> str:
    """Return the configured Sharadar key or reject source-mislabeled requests."""
    if data_source != "sharadar":
        return ""
    key = _resolve_sharadar_key()
    if not key:
        raise HTTPException(
            status_code=422,
            detail="data_source='sharadar' requires a configured Sharadar API key",
        )
    return key


# Shared Sharadar provider factory


def _get_sharadar_provider():
    """Create a SharadarProvider if API key is configured."""
    key = _resolve_sharadar_key()
    if not key:
        return None
    from server.sharadar_provider import SharadarProvider

    return SharadarProvider(api_key=key, cache_dir=runtime.get_data_dir())


# Sharadar API key management


@router.get("/api/config/sharadar-status")
def sharadar_status():
    """Check if a Sharadar API key is configured (never returns the key)."""
    env_key = os.environ.get("SHARADAR_API_KEY", "").strip()
    if env_key:
        return {"configured": True, "source": "env"}
    secrets_file = Path(runtime.get_data_dir()) / ".secrets.json"
    if secrets_file.exists():
        data = runtime._read_json_object(secrets_file, "Secrets file")
        key = data.get("sharadar_api_key", "")
        if not isinstance(key, str):
            raise HTTPException(status_code=500, detail="Secrets file contains an invalid API key value")
        if key.strip():
            return {"configured": True, "source": "file"}
    return {"configured": False, "source": None}


class SharadarKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("API key cannot be empty")
        return clean


@router.post("/api/config/sharadar-key")
async def save_sharadar_key(req: SharadarKeyRequest):
    """Validate and save a Sharadar API key to the server-side secrets file."""
    key = req.api_key

    # Validate against Sharadar API with a lightweight lookup
    test_url = "https://data.nasdaq.com/api/v3/datatables/SHARADAR/TICKERS.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(test_url, params={"ticker": "AAPL", "api_key": key})
        if resp.status_code == 400:
            raise HTTPException(status_code=401, detail="Sharadar rejected the API key")
        if resp.status_code == 403:
            raise HTTPException(status_code=401, detail="Invalid or expired API key")
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Sharadar API returned HTTP {resp.status_code}",
            )
        # Verify the response has actual data
        body = resp.json()
        if not body.get("datatable", {}).get("data"):
            raise HTTPException(
                status_code=401,
                detail="API key valid but no data returned — check your subscription",
            )
    except httpx.HTTPError as exc:
        logger.warning("Sharadar API key validation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not reach Sharadar API") from exc

    # Save to secrets file
    secrets_file = Path(runtime.get_data_dir()) / ".secrets.json"
    try:
        secrets = runtime._read_json_object(secrets_file, "Secrets file") if secrets_file.exists() else {}
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail="Secrets file is invalid; refusing to overwrite it",
        ) from exc
    secrets["sharadar_api_key"] = key
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    config_store._atomic_write_json(secrets_file, secrets, mode=0o600)

    return {"status": "ok", "source": "file"}


@router.delete("/api/config/sharadar-key")
def delete_sharadar_key():
    """Remove the Sharadar API key from the server-side secrets file."""
    env_key = os.environ.get("SHARADAR_API_KEY", "").strip()
    if env_key:
        raise HTTPException(
            status_code=409,
            detail="Cannot remove key set via environment variable. Unset SHARADAR_API_KEY instead.",
        )
    secrets_file = Path(runtime.get_data_dir()) / ".secrets.json"
    if not secrets_file.exists():
        return {"status": "ok"}
    try:
        secrets = runtime._read_json_object(secrets_file, "Secrets file")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail="Secrets file is invalid; key was not removed") from exc
    secrets.pop("sharadar_api_key", None)
    if secrets:
        config_store._atomic_write_json(secrets_file, secrets, mode=0o600)
    else:
        secrets_file.unlink()
    return {"status": "ok"}
