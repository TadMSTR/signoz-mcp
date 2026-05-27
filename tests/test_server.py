"""Tests for signoz_mcp/server.py and signoz_mcp/_client.py."""

from __future__ import annotations

import time

import pytest
import respx
from httpx import Response

# _client module-level validation runs at import time; we need to ensure
# the env vars are set before importing. Patched via conftest / monkeypatch below.


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch):
    """Ensure required env vars are set before each test."""
    monkeypatch.setenv("SIGNOZ_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNOZ_URL", "http://localhost:8080")
    monkeypatch.setenv("SIGNOZ_QUERY_VERSION", "v3")


# ── Time helpers ──────────────────────────────────────────────────────────────


def test_parse_time_ms_now():
    # Import after env is patched
    from signoz_mcp.server import _parse_time_ms

    ms = _parse_time_ms("now")
    assert abs(ms - int(time.time() * 1000)) < 2000


def test_parse_time_ms_relative_hour():
    from signoz_mcp.server import _parse_time_ms

    ms = _parse_time_ms("-1h")
    expected = int(time.time() * 1000) - 3_600_000
    assert abs(ms - expected) < 2000


def test_parse_time_ms_relative_minutes():
    from signoz_mcp.server import _parse_time_ms

    ms = _parse_time_ms("-30m")
    expected = int(time.time() * 1000) - 1_800_000
    assert abs(ms - expected) < 2000


def test_parse_time_ms_invalid():
    from signoz_mcp.server import _parse_time_ms

    with pytest.raises(ValueError):
        _parse_time_ms("not-a-time")


# ── Input validation ──────────────────────────────────────────────────────────


def test_validate_service_accepts_valid():
    from signoz_mcp.server import _validate_service

    assert _validate_service("my-service") == "my-service"
    assert _validate_service("svc_name.v2") == "svc_name.v2"


def test_validate_service_rejects_injection():
    from signoz_mcp.server import _validate_service

    with pytest.raises(ValueError):
        _validate_service("svc'; DROP TABLE--")

    with pytest.raises(ValueError):
        _validate_service("svc AND 1=1")

    with pytest.raises(ValueError):
        _validate_service("svc<script>")


# ── list_services ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_services_returns_list():
    respx.get("http://localhost:8080/api/v1/services").mock(
        return_value=Response(
            200,
            json=[
                {"serviceName": "frontend", "p99": 120.5, "errorRate": 0.01, "callRate": 10.0},
                {"serviceName": "backend", "p99": 80.0, "errorRate": 0.0, "callRate": 50.0},
            ],
        )
    )
    from signoz_mcp.server import list_services

    result = await list_services()
    assert len(result) == 2
    assert result[0]["serviceName"] == "frontend"


# ── count_errors ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_count_errors_returns_sorted_rows():
    respx.post("http://localhost:8080/api/v3/query_range").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "result": [
                        {
                            "metric": {"serviceName": "frontend"},
                            "values": [[1700000000000, "5"], [1700000060000, "3"]],
                        },
                        {
                            "metric": {"serviceName": "backend"},
                            "values": [[1700000000000, "1"]],
                        },
                    ]
                }
            },
        )
    )
    from signoz_mcp.server import count_errors

    rows = await count_errors(start="-1h")
    assert rows[0]["serviceName"] == "frontend"
    assert rows[0]["error_count"] == 8.0
    assert rows[1]["serviceName"] == "backend"
    assert rows[1]["error_count"] == 1.0


@pytest.mark.asyncio
@respx.mock
async def test_count_errors_limit_capped():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json={"data": {"result": []}})

    respx.post("http://localhost:8080/api/v3/query_range").mock(side_effect=capture)
    from signoz_mcp.server import count_errors

    await count_errors(limit=99999)
    import json

    payload = json.loads(captured[0])
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    assert spec["limit"] <= 10000


# ── search_traces ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_search_traces_happy_path():
    respx.post("http://localhost:8080/api/v3/query_range").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "result": [{"traceID": "abc123", "serviceName": "frontend", "hasError": True}]
                }
            },
        )
    )
    from signoz_mcp.server import search_traces

    result = await search_traces(service="frontend", has_error=True)
    assert len(result) == 1
    assert result[0]["traceID"] == "abc123"


@pytest.mark.asyncio
async def test_search_traces_rejects_invalid_service():
    from signoz_mcp.server import search_traces

    with pytest.raises(ValueError):
        await search_traces(service="svc; --inject")


@pytest.mark.asyncio
@respx.mock
async def test_search_traces_limit_capped():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json={"data": {"result": []}})

    respx.post("http://localhost:8080/api/v3/query_range").mock(side_effect=capture)
    from signoz_mcp.server import search_traces

    await search_traces(service="svc", limit=9999)
    import json

    payload = json.loads(captured[0])
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    assert spec["limit"] <= 500


# ── tail_logs ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_tail_logs_happy_path():
    respx.post("http://localhost:8080/api/v3/query_range").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "result": [
                        {"timestamp": 1700000000000, "severityText": "ERROR", "body": "boom"}
                    ]
                }
            },
        )
    )
    from signoz_mcp.server import tail_logs

    result = await tail_logs(service="backend")
    assert len(result) == 1
    assert result[0]["body"] == "boom"


@pytest.mark.asyncio
async def test_tail_logs_rejects_invalid_service():
    from signoz_mcp.server import tail_logs

    with pytest.raises(ValueError):
        await tail_logs(service="../../etc/passwd")


# ── count_log_errors ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_count_log_errors_returns_sorted_rows():
    respx.post("http://localhost:8080/api/v3/query_range").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "result": [
                        {
                            "metric": {"serviceName": "svc-a"},
                            "values": [[1700000000000, "10"]],
                        },
                    ]
                }
            },
        )
    )
    from signoz_mcp.server import count_log_errors

    rows = await count_log_errors()
    assert rows[0]["serviceName"] == "svc-a"
    assert rows[0]["log_error_count"] == 10.0


# ── query_metric ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_query_metric_happy_path():
    respx.post("http://localhost:8080/api/v3/query_range").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "result": [
                        {
                            "metric": {"state": "idle"},
                            "values": [[1700000000000, "0.95"]],
                        }
                    ]
                }
            },
        )
    )
    from signoz_mcp.server import query_metric

    result = await query_metric(metric_name="system_cpu_time")
    assert len(result) == 1
    assert result[0]["metric"]["state"] == "idle"


# ── list_metrics ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_metrics_returns_sorted():
    respx.get("http://localhost:8080/api/v1/metricsNames").mock(
        return_value=Response(200, json=["system_mem", "http_requests", "cpu_time"])
    )
    from signoz_mcp.server import list_metrics

    result = await list_metrics()
    assert result == ["cpu_time", "http_requests", "system_mem"]


# ── list_alert_rules ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_alert_rules_returns_list():
    respx.get("http://localhost:8080/api/v1/rules").mock(
        return_value=Response(
            200,
            json=[{"name": "high-error-rate", "state": "firing"}],
        )
    )
    from signoz_mcp.server import list_alert_rules

    result = await list_alert_rules()
    assert len(result) == 1
    assert result[0]["name"] == "high-error-rate"


# ── get_health ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_health_returns_status():
    respx.get("http://localhost:8080/api/v1/health").mock(
        return_value=Response(200, json={"status": "ok"})
    )
    from signoz_mcp.server import get_health

    result = await get_health()
    assert result["status"] == "ok"


# ── 401 / auth error handling ─────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_auth_error_does_not_leak_key():
    respx.get("http://localhost:8080/api/v1/health").mock(
        return_value=Response(401, json={"error": "unauthenticated"})
    )
    from signoz_mcp.server import get_health

    with pytest.raises(ValueError) as exc_info:
        await get_health()
    # Error message must not contain the API key value
    assert "test-api-key" not in str(exc_info.value)
    assert "SIGNOZ_API_KEY missing or invalid" in str(exc_info.value)


# ── Timeout handling ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_timeout_raises_timeout_error():
    import httpx as _httpx

    respx.get("http://localhost:8080/api/v1/health").mock(
        side_effect=_httpx.TimeoutException("timeout")
    )
    from signoz_mcp.server import get_health

    with pytest.raises(TimeoutError):
        await get_health()
