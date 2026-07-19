"""Tests for signoz_mcp/server.py and signoz_mcp/_client.py.

API compatibility note: signoz-mcp targets the SigNoz v5 query_range API used by
v0.118. The v5 time_series/scalar envelope nests results under
data.data.results[].aggregations[].series[], with labels as a list of
{"key": {"name": ...}, "value": ...} objects and values as
{"timestamp": ..., "value": ...} points. The mock helpers below reproduce that
exact shape so the tests validate against reality, not an assumed schema.
"""

from __future__ import annotations

import json
import time

import pytest
import respx
from httpx import Response

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch):
    """Ensure required env vars are set before each test."""
    monkeypatch.setenv("SIGNOZ_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNOZ_URL", "http://localhost:8080")
    monkeypatch.setenv("SIGNOZ_QUERY_VERSION", "v5")


# ── Time helpers ──────────────────────────────────────────────────────────────


def test_parse_time_ms_now():
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


def test_validate_signal_accepts_and_rejects():
    from signoz_mcp.server import _validate_signal

    assert _validate_signal("Traces") == "traces"
    with pytest.raises(ValueError):
        _validate_signal("bogus")


# ── Helpers: real v5 query_range response shapes ─────────────────────────────


def _v5_series(labels: dict, points: list) -> dict:
    """One v5 aggregation series: labels list + {timestamp,value} points.

    Mirrors the real API: labels are a list of {"key": {"name": ...}, "value": ...}
    objects and each point is a {"timestamp": ..., "value": ...} dict.
    """
    return {
        "labels": [{"key": {"name": k}, "value": v} for k, v in labels.items()],
        "values": [{"timestamp": ts, "value": val} for ts, val in points],
    }


def _v5_agg(series: list[dict], alias: str = "__result_0") -> dict:
    """A v5 aggregation object. Alias is backend-assigned ('__result_0')."""
    return {"index": 0, "alias": alias, "meta": {}, "series": series}


def _v5_time_series(aggregations: list[dict]) -> dict:
    """Build a v5 query_range time_series response from aggregation objects."""
    return {
        "status": "success",
        "data": {
            "type": "time_series",
            "data": {"results": [{"queryName": "A", "aggregations": aggregations}]},
        },
    }


def _v5_raw(rows: list[dict]) -> dict:
    """Build a v5 query_range raw response; each row is wrapped in {'data': ...}."""
    return {
        "status": "success",
        "data": {
            "type": "raw",
            "data": {
                "results": [
                    {"queryName": "A", "nextCursor": "", "rows": [{"data": r} for r in rows]}
                ]
            },
        },
    }


def _v5_trace(rows: list[dict]) -> dict:
    """Build a v5 query_range trace response; each row is wrapped in {'data': ...}."""
    return {
        "status": "success",
        "data": {
            "type": "trace",
            "data": {
                "results": [
                    {"queryName": "A", "nextCursor": "", "rows": [{"data": r} for r in rows]}
                ]
            },
        },
    }


# ── list_services ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_services_returns_list():
    respx.get("http://localhost:8080/api/v1/services/list").mock(
        return_value=Response(200, json=["frontend", "backend"])
    )
    from signoz_mcp.server import list_services

    result = await list_services()
    assert result == ["frontend", "backend"]


# ── count_errors ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_count_errors_returns_sorted_rows():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(
            200,
            json=_v5_time_series(
                [
                    _v5_agg(
                        [
                            _v5_series(
                                {"serviceName": "frontend"},
                                [(1700000000000, 5), (1700000060000, 3)],
                            ),
                            _v5_series({"serviceName": "backend"}, [(1700000000000, 1)]),
                        ]
                    )
                ]
            ),
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
        return Response(200, json=_v5_time_series([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import count_errors

    await count_errors(limit=99999)
    payload = json.loads(captured[0])
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    assert spec["limit"] <= 10000


@pytest.mark.asyncio
@respx.mock
async def test_count_errors_uses_v5_endpoint():
    captured = []

    def capture(request):
        captured.append(request)
        return Response(200, json=_v5_time_series([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import count_errors

    await count_errors()
    assert captured, "v5 query_range was not called"
    assert "/api/v5/query_range" in str(captured[0].url)


# ── search_traces ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_search_traces_happy_path():
    trace_rows = [{"trace_id": "abc123", "service.name": "frontend", "span_count": 3}]
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_trace(trace_rows))
    )
    from signoz_mcp.server import search_traces

    result = await search_traces(service="frontend", has_error=True)
    assert len(result) == 1
    assert result[0]["trace_id"] == "abc123"


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
        return Response(200, json=_v5_trace([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import search_traces

    await search_traces(service="svc", limit=9999)
    payload = json.loads(captured[0])
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    assert spec["limit"] <= 500


@pytest.mark.asyncio
@respx.mock
async def test_search_traces_uses_trace_request_type():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_trace([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import search_traces

    await search_traces(service="svc")
    payload = json.loads(captured[0])
    assert payload["requestType"] == "trace"


# ── tail_logs ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_tail_logs_happy_path():
    log_rows = [{"timestamp": 1700000000000, "severity_text": "ERROR", "body": "boom"}]
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_raw(log_rows))
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


@pytest.mark.asyncio
async def test_tail_logs_rejects_invalid_severity():
    from signoz_mcp.server import tail_logs

    with pytest.raises(ValueError):
        await tail_logs(service="backend", severity="ERROR' OR 1=1 --")

    with pytest.raises(ValueError):
        await tail_logs(service="backend", severity="INVALID")


@pytest.mark.asyncio
@respx.mock
async def test_tail_logs_uses_severity_text_filter():
    """tail_logs must filter on severity_text (v5 logs field name)."""
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_raw([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import tail_logs

    await tail_logs(service="backend", severity="warn")
    payload = json.loads(captured[0])
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    assert "severity_text" in spec["filter"]["expression"]
    assert "WARN" in spec["filter"]["expression"]


# ── count_log_errors ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_count_log_errors_returns_sorted_rows():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(
            200,
            json=_v5_time_series(
                [_v5_agg([_v5_series({"resource.service.name": "svc-a"}, [(1700000000000, 10)])])]
            ),
        )
    )
    from signoz_mcp.server import count_log_errors

    rows = await count_log_errors()
    assert rows[0]["serviceName"] == "svc-a"
    assert rows[0]["log_error_count"] == 10.0


@pytest.mark.asyncio
@respx.mock
async def test_count_log_errors_uses_resource_service_name_groupby():
    """groupBy must use resource.service.name (v5 logs field, not v3 serviceName)."""
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_time_series([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import count_log_errors

    await count_log_errors()
    payload = json.loads(captured[0])
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    group_names = [g["name"] for g in spec.get("groupBy", [])]
    assert "resource.service.name" in group_names
    assert "serviceName" not in group_names


# ── query_metric ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_query_metric_happy_path():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(
            200,
            json=_v5_time_series(
                [_v5_agg([_v5_series({"state": "idle"}, [(1700000000000, 0.95)])])]
            ),
        )
    )
    from signoz_mcp.server import query_metric

    result = await query_metric(metric_name="system_cpu_time")
    assert len(result) == 1
    assert result[0]["labels"]["state"] == "idle"
    assert result[0]["values"][0]["value"] == 0.95


@pytest.mark.asyncio
@respx.mock
async def test_query_metric_uses_v5_aggregation_format():
    """v5 metrics: metricName must be inside the aggregation object."""
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_time_series([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import query_metric

    await query_metric(metric_name="system_cpu_time")
    payload = json.loads(captured[0])
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    # metricName must NOT be at the top level of spec
    assert "metricName" not in spec, "metricName must not be at spec top level in v5"
    # metricName must be inside the aggregation
    agg = spec["aggregations"][0]
    assert agg["metricName"] == "system_cpu_time"
    assert "timeAggregation" in agg
    assert "spaceAggregation" in agg


@pytest.mark.asyncio
async def test_query_metric_rejects_invalid_name():
    from signoz_mcp.server import query_metric

    with pytest.raises(ValueError):
        await query_metric(metric_name="my metric; DROP")


@pytest.mark.asyncio
async def test_query_metric_rejects_long_label_filter():
    from signoz_mcp.server import query_metric

    with pytest.raises(ValueError):
        await query_metric(metric_name="my_metric", label_filter="x" * 501)


@pytest.mark.asyncio
async def test_query_metric_rejects_disallowed_label_filter_chars():
    from signoz_mcp.server import query_metric

    with pytest.raises(ValueError):
        await query_metric(metric_name="my_metric", label_filter="state = 'idle'; --inject")


@pytest.mark.asyncio
@respx.mock
async def test_query_metric_missing_metric_raises_clean_error():
    """A 404 'could not find the metric' surfaces as a clean ValueError, not a raw httpx error."""
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(
            404,
            json={
                "status": "error",
                "error": {"code": "not_found", "message": "could not find the metric foo"},
            },
        )
    )
    from signoz_mcp.server import query_metric

    with pytest.raises(ValueError) as exc_info:
        await query_metric(metric_name="foo")
    assert "could not find the metric foo" in str(exc_info.value)
    assert "test-api-key" not in str(exc_info.value)


# ── list_metrics ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_metrics_returns_metric_list():
    respx.get("http://localhost:8080/api/v2/metrics").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "metrics": [
                        {
                            "metricName": "system_cpu_time",
                            "type": "sum",
                            "temporality": "cumulative",
                            "isMonotonic": True,
                        }
                    ]
                }
            },
        )
    )
    from signoz_mcp.server import list_metrics

    result = await list_metrics(search_text="cpu")
    assert isinstance(result, list)
    assert result[0]["metricName"] == "system_cpu_time"


@pytest.mark.asyncio
@respx.mock
async def test_list_metrics_uses_v2_endpoint_and_params():
    captured = []

    def capture(request):
        captured.append(request)
        return Response(200, json={"data": {"metrics": []}})

    respx.get("http://localhost:8080/api/v2/metrics").mock(side_effect=capture)
    from signoz_mcp.server import list_metrics

    await list_metrics(search_text="mem", limit=5)
    assert captured, "v2 metrics endpoint was not called"
    url = str(captured[0].url)
    assert "/api/v2/metrics" in url
    assert "searchText=mem" in url


@pytest.mark.asyncio
async def test_list_metrics_rejects_bad_search_text():
    from signoz_mcp.server import list_metrics

    with pytest.raises(ValueError):
        await list_metrics(search_text="cpu'; DROP--")


# ── get_field_keys / get_field_values ─────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_field_keys_happy_path():
    respx.get("http://localhost:8080/api/v1/fields/keys").mock(
        return_value=Response(
            200,
            json={
                "status": "success",
                "data": {
                    "keys": {"service.name": [{"name": "service.name", "fieldContext": "resource"}]}
                },
            },
        )
    )
    from signoz_mcp.server import get_field_keys

    result = await get_field_keys(signal="traces", search_text="service")
    assert "keys" in result
    assert "service.name" in result["keys"]


@pytest.mark.asyncio
async def test_get_field_keys_rejects_bad_signal():
    from signoz_mcp.server import get_field_keys

    with pytest.raises(ValueError):
        await get_field_keys(signal="bogus")


@pytest.mark.asyncio
@respx.mock
async def test_get_field_values_happy_path():
    respx.get("http://localhost:8080/api/v1/fields/values").mock(
        return_value=Response(
            200,
            json={
                "status": "success",
                "data": {"values": {"stringValues": ["frontend", "backend"]}, "complete": True},
            },
        )
    )
    from signoz_mcp.server import get_field_values

    result = await get_field_values(signal="traces", name="service.name")
    assert result["values"]["stringValues"] == ["frontend", "backend"]


@pytest.mark.asyncio
async def test_get_field_values_requires_name():
    from signoz_mcp.server import get_field_values

    with pytest.raises(ValueError):
        await get_field_values(signal="traces", name="")


@pytest.mark.asyncio
async def test_get_field_values_rejects_injection_name():
    from signoz_mcp.server import get_field_values

    with pytest.raises(ValueError):
        await get_field_values(signal="traces", name="svc' OR 1=1")


# ── list_alert_rules ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_alert_rules_returns_list():
    respx.get("http://localhost:8080/api/v1/rules").mock(
        return_value=Response(
            200,
            json={"data": {"rules": [{"name": "high-error-rate", "state": "firing"}]}},
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


# ── _client payload shape ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_client_omits_variables_field():
    """v5 query_range payload must not include the v3-era 'variables' top-level field."""
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_time_series([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import count_errors

    await count_errors()
    payload = json.loads(captured[0])
    assert "variables" not in payload, "'variables' field was removed in v5"
