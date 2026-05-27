"""Shared httpx client and query helper for the SigNoz HTTP API."""

from __future__ import annotations

import os
import time

import httpx
import structlog

_log = structlog.get_logger("signoz-mcp")

_ALLOWED_QUERY_VERSIONS = frozenset({"v3", "v5"})

SIGNOZ_URL = os.environ.get("SIGNOZ_URL", "http://localhost:8080").rstrip("/")
SIGNOZ_API_KEY = os.environ.get("SIGNOZ_API_KEY", "")
SIGNOZ_QUERY_VERSION = os.environ.get("SIGNOZ_QUERY_VERSION", "v3")

if SIGNOZ_QUERY_VERSION not in _ALLOWED_QUERY_VERSIONS:
    raise RuntimeError(
        f"SIGNOZ_QUERY_VERSION must be one of {sorted(_ALLOWED_QUERY_VERSIONS)!r}, "
        f"got: {SIGNOZ_QUERY_VERSION!r}"
    )

if not SIGNOZ_API_KEY:
    raise RuntimeError("SIGNOZ_API_KEY is required but not set")

_QUERY_URL = f"{SIGNOZ_URL}/api/{SIGNOZ_QUERY_VERSION}/query_range"
_HEADERS = {
    "SIGNOZ-API-KEY": SIGNOZ_API_KEY,
    "Content-Type": "application/json",
}

_HTTP_TIMEOUT = 30.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _build_query_payload(
    signal: str,
    request_type: str,
    spec: dict,
    start_ms: int,
    end_ms: int,
) -> dict:
    return {
        "start": start_ms,
        "end": end_ms,
        "requestType": request_type,
        "variables": {},
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {"name": "A", "signal": signal, "disabled": False, **spec},
                }
            ]
        },
    }


async def query(
    signal: str,
    request_type: str,
    spec: dict,
    start_ms: int,
    end_ms: int,
) -> dict:
    """POST to /api/{version}/query_range and return the parsed JSON body.

    Raises ValueError with a sanitized message on auth failures.
    Raises TimeoutError on request timeout.
    Never includes the API key value in any raised exception.
    """
    payload = _build_query_payload(signal, request_type, spec, start_ms, end_ms)
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_QUERY_URL, headers=_HEADERS, json=payload)
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"SigNoz did not respond within {_HTTP_TIMEOUT}s") from exc
    except httpx.ConnectError as exc:
        raise ConnectionError(f"Could not connect to SigNoz at {SIGNOZ_URL}") from exc

    if resp.status_code == 401:
        raise ValueError("SIGNOZ_API_KEY missing or invalid")
    resp.raise_for_status()
    return resp.json()


async def get(path: str) -> dict:
    """GET a SigNoz API endpoint and return the parsed JSON body."""
    url = f"{SIGNOZ_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=_HEADERS)
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"SigNoz did not respond within {_HTTP_TIMEOUT}s") from exc
    except httpx.ConnectError as exc:
        raise ConnectionError(f"Could not connect to SigNoz at {SIGNOZ_URL}") from exc

    if resp.status_code == 401:
        raise ValueError("SIGNOZ_API_KEY missing or invalid")
    resp.raise_for_status()
    return resp.json()
