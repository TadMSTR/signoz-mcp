"""Shared httpx client and query helper for the SigNoz HTTP API."""

from __future__ import annotations

import os
import time

import httpx
import structlog

_log = structlog.get_logger("signoz-mcp")

# v3/v4 were superseded by v5 in SigNoz v0.118; only v5 is supported.
_ALLOWED_QUERY_VERSIONS = frozenset({"v5"})

SIGNOZ_URL = os.environ.get("SIGNOZ_URL", "http://localhost:8080").rstrip("/")
SIGNOZ_API_KEY = os.environ.get("SIGNOZ_API_KEY", "")
SIGNOZ_QUERY_VERSION = os.environ.get("SIGNOZ_QUERY_VERSION", "v5")

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


def _signoz_error_message(resp: httpx.Response) -> str:
    """Extract SigNoz's structured error message from a non-2xx response.

    SigNoz returns either {"error": {"code": ..., "message": ...}} (v5) or
    {"error": "..."} (v1). It never echoes the request headers, so the API key
    can never appear here. Falls back to a bare status code.
    """
    try:
        body = resp.json()
    except Exception:  # body may not be JSON (e.g. an HTML error page)
        return f"HTTP {resp.status_code}"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or f"HTTP {resp.status_code}")
    if isinstance(err, str) and err:
        return err
    return f"HTTP {resp.status_code}"


def _check_response(resp: httpx.Response) -> None:
    """Raise a sanitized error for non-2xx responses; never leak the API key.

    A 401 maps to a fixed auth message. Any other 4xx/5xx surfaces SigNoz's own
    error text (e.g. "could not find the metric X" for a 404), which is far more
    actionable than httpx's default "Client error ... for url ..." message and
    avoids echoing the internal SigNoz URL.
    """
    if resp.status_code == 401:
        raise ValueError("SIGNOZ_API_KEY missing or invalid")
    if resp.status_code >= 400:
        raise ValueError(f"SigNoz request failed: {_signoz_error_message(resp)}")


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

    Raises ValueError with a sanitized message on auth or query errors.
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

    _check_response(resp)
    return resp.json()


async def get(path: str, params: dict | None = None) -> dict:
    """GET a SigNoz API endpoint and return the parsed JSON body.

    Args:
        path:   API path beginning with '/', e.g. '/api/v2/metrics'.
        params: Optional query-string parameters (httpx URL-encodes them).
    """
    url = f"{SIGNOZ_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=_HEADERS, params=params)
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"SigNoz did not respond within {_HTTP_TIMEOUT}s") from exc
    except httpx.ConnectError as exc:
        raise ConnectionError(f"Could not connect to SigNoz at {SIGNOZ_URL}") from exc

    _check_response(resp)
    return resp.json()
