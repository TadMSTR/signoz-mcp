"""Tests for src/signoz_mcp/server.py and src/signoz_mcp/_client.py.

API compatibility note: signoz-mcp targets the SigNoz v5 query_range API used by
v0.118. The mock helpers below reproduce the real response envelopes:
  - time_series: data.data.results[].aggregations[].series[] with labels as a
    list of {"key": {"name": ...}, "value": ...} and values as
    {"timestamp": ..., "value": ...} points (backend-assigned alias "__result_0").
  - scalar: data.data.results[].columns + .data (a column-aligned table).
  - raw/trace: data.data.results[].rows[] where each row is {"data": {...}}.
So the tests validate against reality, not an assumed schema.
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


# Log field-key payloads, shaped from the real SigNoz v0.118.0 response.
#
# _BUILTIN_ONLY_LOG_KEYS is what forge returns TODAY with zero ingested logs: eight
# keys, all at fieldContext 'log' or 'scope'. It is deliberately NOT empty — that is
# the whole point, and a guard testing for emptiness would never fire against it.
_BUILTIN_ONLY_LOG_KEYS = {
    "data": {
        "keys": {
            "body": [{"name": "body", "signal": "logs", "fieldContext": "log"}],
            "severity_text": [{"name": "severity_text", "signal": "logs", "fieldContext": "log"}],
            "scope_name": [{"name": "scope_name", "signal": "logs", "fieldContext": "scope"}],
        }
    }
}

_POPULATED_LOG_KEYS = {
    "data": {
        "keys": {
            "body": [{"name": "body", "signal": "logs", "fieldContext": "log"}],
            "service.name": [
                {"name": "service.name", "signal": "logs", "fieldContext": "resource"}
            ],
        }
    }
}


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


def test_validate_filter_expr_allows_dashes_and_paths():
    """Real service names have dashes; log body filters have slashes — both allowed."""
    from signoz_mcp.server import _validate_filter_expr

    assert _validate_filter_expr("service.name = 'scoped-mcp-developer'")
    assert _validate_filter_expr("body CONTAINS '/api/v5'")
    assert _validate_filter_expr("severity_text IN ['ERROR', 'WARN']")


def test_validate_filter_expr_rejects_dangerous_chars():
    from signoz_mcp.server import _validate_filter_expr

    with pytest.raises(ValueError):
        _validate_filter_expr("a = 1; DROP TABLE")  # semicolon
    with pytest.raises(ValueError):
        _validate_filter_expr("a = `whoami`")  # backtick
    with pytest.raises(ValueError):
        _validate_filter_expr("x" * 1001)  # too long


def test_build_agg_expression():
    from signoz_mcp.server import _build_agg_expression

    assert _build_agg_expression("count", "") == "count()"
    assert _build_agg_expression("rate", "") == "rate()"
    assert _build_agg_expression("p99", "duration_nano") == "p99(duration_nano)"
    with pytest.raises(ValueError):
        _build_agg_expression("bogus", "x")
    with pytest.raises(ValueError):
        _build_agg_expression("p99", "")  # requires aggregate_on


# ── Helpers: real v5 query_range response shapes ─────────────────────────────


def _v5_series(labels: dict, points: list) -> dict:
    return {
        "labels": [{"key": {"name": k}, "value": v} for k, v in labels.items()],
        "values": [{"timestamp": ts, "value": val} for ts, val in points],
    }


def _v5_agg(series: list[dict], alias: str = "__result_0") -> dict:
    return {"index": 0, "alias": alias, "meta": {}, "series": series}


def _v5_time_series(aggregations: list[dict]) -> dict:
    return {
        "status": "success",
        "data": {
            "type": "time_series",
            "data": {"results": [{"queryName": "A", "aggregations": aggregations}]},
        },
    }


def _v5_scalar(columns: list[str], rows: list[list]) -> dict:
    return {
        "status": "success",
        "data": {
            "type": "scalar",
            "data": {
                "results": [
                    {"queryName": "A", "columns": [{"name": c} for c in columns], "data": rows}
                ]
            },
        },
    }


def _v5_raw(rows: list[dict]) -> dict:
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


# vikunja#322. The version of this test that shipped with the defect mocked
# GET /api/v1/services/list and asserted the two names came back — it passed
# against the broken implementation and could not have failed for the reason the
# tool was wrong. Retargeted onto the replacement rather than deleted; the
# coverage it represented is real, it was just pointed at the wrong endpoint.
#
# These are CONTRACT tests. They cannot prove the result is COMPLETE, because a
# mock returns whatever it is told to — that claim is only testable against a
# live backend, which is what tests/test_live_signoz.py does. What they can and
# do pin is the three things that were wrong or would silently break again:
# the endpoint, the HTTP verb, and the nanoseconds-as-strings encoding.

_SERVICES_FIXTURE = [
    {
        "serviceName": "frontend",
        "p99": 1093332.1,
        "avgDuration": 653861.35,
        "numCalls": 14,
        "callRate": 2.3e-05,
        "numErrors": 0,
        "errorRate": 0.0,
        "num4XX": 0,
        "fourXXRate": 0.0,
        "dataWarning": {"topLevelOps": ["overflow_operation", "GET /"]},
    },
    {
        "serviceName": "backend",
        "p99": 19356.26,
        "avgDuration": 12242.5,
        "numCalls": 4,
        "callRate": 6.6e-06,
        "numErrors": 1,
        "errorRate": 0.25,
        "num4XX": 0,
        "fourXXRate": 0.0,
        "dataWarning": {"topLevelOps": ["overflow_operation"]},
    },
]


@pytest.mark.asyncio
@respx.mock
async def test_list_services_uses_the_time_bounded_post_endpoint():
    """The defect itself: the old GET has no time range and under-reports."""
    old_route = respx.get("http://localhost:8080/api/v1/services/list").mock(
        return_value=Response(200, json=["frontend", "backend"])
    )
    new_route = respx.post("http://localhost:8080/api/v1/services").mock(
        return_value=Response(200, json=_SERVICES_FIXTURE)
    )
    from signoz_mcp.server import list_services

    result = await list_services(start="-24h")

    assert new_route.called, "list_services must POST /api/v1/services"
    assert not old_route.called, (
        "list_services still calls GET /api/v1/services/list, which takes no time "
        "range and returned 16 of 25 services on forge (vikunja#322)"
    )
    assert [s["serviceName"] for s in result] == ["frontend", "backend"]


@pytest.mark.asyncio
@respx.mock
async def test_list_services_sends_nanoseconds_as_json_strings():
    """Numbers here return 400 from SigNoz. Confirmed live 2026-09-20."""
    route = respx.post("http://localhost:8080/api/v1/services").mock(
        return_value=Response(200, json=_SERVICES_FIXTURE)
    )
    from signoz_mcp.server import list_services

    await list_services(start="-24h", end="now")

    body = json.loads(route.calls[0].request.content)
    assert isinstance(body["start"], str), (
        "start must be a JSON STRING of nanoseconds — a number returns 400 "
        "'cannot unmarshal number into Go struct field GetServicesParams.start'"
    )
    assert isinstance(body["end"], str), "end must be a JSON string of nanoseconds"
    # Nanoseconds, not milliseconds: a 24h window is 8.64e13 ns apart.
    span_ns = int(body["end"]) - int(body["start"])
    assert abs(span_ns - 24 * 3600 * 1_000_000_000) < 5_000_000_000, (
        f"window is {span_ns} ns apart; a -24h request should be ~8.64e13 ns. "
        "A millisecond value here silently queries a 24-second window."
    )


@pytest.mark.asyncio
@respx.mock
async def test_list_services_returns_red_metrics_and_drops_datawarning():
    respx.post("http://localhost:8080/api/v1/services").mock(
        return_value=Response(200, json=_SERVICES_FIXTURE)
    )
    from signoz_mcp.server import list_services

    result = await list_services(start="-24h")

    assert result[1] == {
        "serviceName": "backend",
        "p99": 19356.26,
        "avgDuration": 12242.5,
        "numCalls": 4,
        "callRate": 6.6e-06,
        "numErrors": 1,
        "errorRate": 0.25,
        "num4XX": 0,
        "fourXXRate": 0.0,
    }
    assert all("dataWarning" not in s for s in result)


@pytest.mark.asyncio
@respx.mock
async def test_list_services_tolerates_a_non_list_body():
    respx.post("http://localhost:8080/api/v1/services").mock(
        return_value=Response(200, json={"error": None})
    )
    from signoz_mcp.server import list_services

    assert await list_services(start="-24h") == []


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
@respx.mock
async def test_search_traces_combines_filter_and_shortcuts():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_trace([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import search_traces

    await search_traces(
        filter="http.status_code = 500", service="frontend", has_error=True, min_duration_ms=5
    )
    spec = json.loads(captured[0])["compositeQuery"]["queries"][0]["spec"]
    expr = spec["filter"]["expression"]
    assert "http.status_code = 500" in expr
    assert "service.name = 'frontend'" in expr
    assert "has_error = true" in expr
    assert "duration_nano >= 5000000" in expr
    assert " AND " in expr


@pytest.mark.asyncio
async def test_search_traces_rejects_invalid_service():
    from signoz_mcp.server import search_traces

    with pytest.raises(ValueError):
        await search_traces(service="svc; --inject")


@pytest.mark.asyncio
async def test_search_traces_rejects_bad_filter():
    from signoz_mcp.server import search_traces

    with pytest.raises(ValueError):
        await search_traces(filter="a = 1; DROP TABLE")


@pytest.mark.asyncio
async def test_search_traces_rejects_operation_with_quotes():
    """operation must not be able to break out of its 'name = <op>' string literal."""
    from signoz_mcp.server import search_traces

    with pytest.raises(ValueError):
        await search_traces(operation="x' OR has_error='true")


@pytest.mark.asyncio
@respx.mock
async def test_search_traces_accepts_realistic_operation():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_trace([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import search_traces

    await search_traces(operation="tools/call system-ops_run_command")
    expr = json.loads(captured[0])["compositeQuery"]["queries"][0]["spec"]["filter"]["expression"]
    assert "name = 'tools/call system-ops_run_command'" in expr


@pytest.mark.asyncio
@respx.mock
async def test_search_traces_limit_capped_and_trace_request_type():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_trace([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import search_traces

    await search_traces(service="svc", limit=9999)
    payload = json.loads(captured[0])
    assert payload["requestType"] == "trace"
    assert payload["compositeQuery"]["queries"][0]["spec"]["limit"] <= 500


# ── aggregate_traces ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_aggregate_traces_scalar_parses_table():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(
            200,
            json=_v5_scalar(["service.name", "__result_0"], [["svc-a", 1835], ["svc-b", 932]]),
        )
    )
    from signoz_mcp.server import aggregate_traces

    rows = await aggregate_traces(aggregation="count", group_by="service.name")
    assert rows[0] == {"service.name": "svc-a", "__result_0": 1835}
    assert rows[1]["service.name"] == "svc-b"


@pytest.mark.asyncio
@respx.mock
async def test_aggregate_traces_builds_expression_and_groupby():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_scalar([], []))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import aggregate_traces

    await aggregate_traces(
        aggregation="p99", aggregate_on="duration_nano", group_by="service.name,name"
    )
    payload = json.loads(captured[0])
    spec = payload["compositeQuery"]["queries"][0]["spec"]
    assert payload["requestType"] == "scalar"
    assert spec["aggregations"][0]["expression"] == "p99(duration_nano)"
    assert [g["name"] for g in spec["groupBy"]] == ["service.name", "name"]


@pytest.mark.asyncio
@respx.mock
async def test_aggregate_traces_time_series_shape():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(
            200,
            json=_v5_time_series(
                [_v5_agg([_v5_series({"service.name": "svc-a"}, [(1700000000000, 3)])])]
            ),
        )
    )
    from signoz_mcp.server import aggregate_traces

    rows = await aggregate_traces(
        aggregation="count", group_by="service.name", request_type="time_series"
    )
    assert rows[0]["labels"]["service.name"] == "svc-a"
    assert rows[0]["values"][0]["value"] == 3


@pytest.mark.asyncio
async def test_aggregate_traces_rejects_bad_inputs():
    from signoz_mcp.server import aggregate_traces

    with pytest.raises(ValueError):
        await aggregate_traces(aggregation="bogus")
    with pytest.raises(ValueError):
        await aggregate_traces(aggregation="p99")  # missing aggregate_on
    with pytest.raises(ValueError):
        await aggregate_traces(aggregation="count", request_type="raw")  # bad request_type


# ── get_trace_details ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_trace_details_returns_spans():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(
            200, json=_v5_raw([{"span_id": "s1", "trace_id": "abcdef"}, {"span_id": "s2"}])
        )

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import get_trace_details

    rows = await get_trace_details(trace_id="abcdef123456")
    assert len(rows) == 2
    assert rows[0]["span_id"] == "s1"
    # include_spans=True must use the 'raw' request type (individual spans)
    assert json.loads(captured[0])["requestType"] == "raw"


@pytest.mark.asyncio
@respx.mock
async def test_get_trace_details_summary_uses_trace_type():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_trace([{"trace_id": "abcdef", "span_count": 5}]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import get_trace_details

    await get_trace_details(trace_id="abcdef", include_spans=False)
    assert json.loads(captured[0])["requestType"] == "trace"


@pytest.mark.asyncio
async def test_get_trace_details_rejects_invalid_id():
    from signoz_mcp.server import get_trace_details

    with pytest.raises(ValueError):
        await get_trace_details(trace_id="not-hex!!")


# ── tail_logs ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_tail_logs_happy_path():
    log_rows = [{"timestamp": 1700000000000, "severity_text": "ERROR", "body": "boom"}]
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_raw(log_rows))
    )
    from signoz_mcp.server import tail_logs

    result = await tail_logs()
    assert len(result) == 1
    assert result[0]["body"] == "boom"


@pytest.mark.asyncio
async def test_tail_logs_takes_no_service_parameter():
    """vikunja#927 — `service` was required, validated, and then never used.

    This replaces test_tail_logs_rejects_invalid_service, which asserted the
    validation of a parameter that had no effect on the query. That test passed
    against the defect: validating an argument you then discard is precisely the
    bug, so a test of the validation alone could never have caught it.
    """
    import inspect

    from signoz_mcp.server import tail_logs

    assert "service" not in inspect.signature(tail_logs).parameters

    with pytest.raises(TypeError):
        await tail_logs(service="backend")


@pytest.mark.asyncio
async def test_tail_logs_rejects_invalid_severity():
    from signoz_mcp.server import tail_logs

    with pytest.raises(ValueError):
        await tail_logs(severity="ERROR' OR 1=1 --")

    with pytest.raises(ValueError):
        await tail_logs(severity="INVALID")


@pytest.mark.asyncio
@respx.mock
async def test_tail_logs_uses_severity_text_filter():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_raw([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    # The empty result now consults the field keys to tell "no matches" from "no log
    # data at all" (vikunja#926). Report a populated signal so this test stays about
    # the filter expression.
    respx.get("http://localhost:8080/api/v1/fields/keys").mock(
        return_value=Response(200, json=_POPULATED_LOG_KEYS)
    )
    from signoz_mcp.server import tail_logs

    await tail_logs(severity="warn")
    spec = json.loads(captured[0])["compositeQuery"]["queries"][0]["spec"]
    assert "severity_text" in spec["filter"]["expression"]
    assert "WARN" in spec["filter"]["expression"]


# ── Empty log store vs. no matching logs (vikunja#926) ────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_empty_result_names_926_when_nothing_has_been_ingested():
    """The whole point: [] must stop meaning two different things.

    An agent that cannot tell "no matching logs" from "this backend holds no logs"
    diagnoses the former and never finds the latter — which is what vikunja#909
    was, parse errors debugged against an empty table.
    """
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_raw([]))
    )
    respx.get("http://localhost:8080/api/v1/fields/keys").mock(
        return_value=Response(200, json=_BUILTIN_ONLY_LOG_KEYS)
    )
    from signoz_mcp.server import tail_logs

    with pytest.raises(ValueError) as exc_info:
        await tail_logs()
    assert "vikunja#926" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_empty_result_is_returned_plainly_when_the_store_has_data():
    """The other side of the guard — it must not fire on a real empty result."""
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_raw([]))
    )
    respx.get("http://localhost:8080/api/v1/fields/keys").mock(
        return_value=Response(200, json=_POPULATED_LOG_KEYS)
    )
    from signoz_mcp.server import tail_logs

    assert await tail_logs() == []


@pytest.mark.asyncio
@respx.mock
async def test_search_logs_empty_result_also_names_926():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_raw([]))
    )
    respx.get("http://localhost:8080/api/v1/fields/keys").mock(
        return_value=Response(200, json=_BUILTIN_ONLY_LOG_KEYS)
    )
    from signoz_mcp.server import search_logs

    with pytest.raises(ValueError) as exc_info:
        await search_logs(severity="ERROR")
    assert "vikunja#926" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_the_guard_reads_field_context_not_key_count():
    """Guards against the version of this check that could never fire.

    The obvious implementation — "are there any field keys at all?" — would be
    permanently satisfied, because SigNoz reports its built-in log schema on an
    empty store. Measured on forge 2026-09-20: 8 keys, all at fieldContext
    'log'/'scope', zero at 'resource'/'attribute'. This fixture has keys; the
    guard must still fire.
    """
    assert _BUILTIN_ONLY_LOG_KEYS["data"]["keys"], "fixture must be non-empty to mean anything"
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_raw([]))
    )
    respx.get("http://localhost:8080/api/v1/fields/keys").mock(
        return_value=Response(200, json=_BUILTIN_ONLY_LOG_KEYS)
    )
    from signoz_mcp.server import tail_logs

    with pytest.raises(ValueError):
        await tail_logs()


# ── search_logs ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_search_logs_builds_filter():
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_raw([{"body": "boom"}]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import search_logs

    result = await search_logs(severity="error", search_text="boom")
    assert result[0]["body"] == "boom"
    expr = json.loads(captured[0])["compositeQuery"]["queries"][0]["spec"]["filter"]["expression"]
    assert "severity_text = 'ERROR'" in expr
    assert "body CONTAINS 'boom'" in expr


@pytest.mark.asyncio
async def test_search_logs_rejects_bad_severity():
    from signoz_mcp.server import search_logs

    with pytest.raises(ValueError):
        await search_logs(severity="NOPE")


# ── aggregate_logs ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_aggregate_logs_scalar():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(
            200, json=_v5_scalar(["severity_text", "__result_0"], [["ERROR", 12]])
        )
    )
    from signoz_mcp.server import aggregate_logs

    rows = await aggregate_logs(
        aggregation="count", group_by="severity_text", filter="severity_text IN ['ERROR', 'WARN']"
    )
    assert rows[0] == {"severity_text": "ERROR", "__result_0": 12}


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
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_time_series([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import query_metric

    await query_metric(metric_name="system_cpu_time")
    spec = json.loads(captured[0])["compositeQuery"]["queries"][0]["spec"]
    assert "metricName" not in spec, "metricName must not be at spec top level in v5"
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
        await query_metric(metric_name="my_metric", label_filter="x" * 1001)


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
                "data": {"keys": {"service.name": [{"name": "service.name"}]}},
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
async def test_get_field_keys_rejects_bad_field_context_and_type():
    from signoz_mcp.server import get_field_keys

    with pytest.raises(ValueError):
        await get_field_keys(signal="traces", field_context="bogus")
    with pytest.raises(ValueError):
        await get_field_keys(signal="traces", field_data_type="notatype")


@pytest.mark.asyncio
async def test_get_field_values_rejects_bad_field_context():
    from signoz_mcp.server import get_field_values

    with pytest.raises(ValueError):
        await get_field_values(signal="traces", name="service.name", field_context="bogus")


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


# ── error / auth handling ─────────────────────────────────────────────────────


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


# ── _client.post() — the helper added for vikunja#322 ─────────────────────────
#
# `query()` is hardcoded to the query_range URL and `get()` cannot carry a body, so
# #322's fix needed a third entry point. It must hold the SAME contract as the other
# two: sanitized errors, and the API key never in an exception. Tested here rather
# than assumed, because it is the newest path to the backend and the one a future
# tool is most likely to reuse.


@pytest.mark.asyncio
@respx.mock
async def test_post_raises_timeout_error():
    import httpx as _httpx

    respx.post("http://localhost:8080/api/v1/services").mock(
        side_effect=_httpx.TimeoutException("timeout")
    )
    from signoz_mcp.server import list_services

    with pytest.raises(TimeoutError):
        await list_services(start="-24h")


@pytest.mark.asyncio
@respx.mock
async def test_post_raises_connection_error():
    import httpx as _httpx

    respx.post("http://localhost:8080/api/v1/services").mock(
        side_effect=_httpx.ConnectError("refused")
    )
    from signoz_mcp.server import list_services

    with pytest.raises(ConnectionError):
        await list_services(start="-24h")


@pytest.mark.asyncio
@respx.mock
async def test_post_401_does_not_leak_the_api_key():
    respx.post("http://localhost:8080/api/v1/services").mock(
        return_value=Response(401, json={"error": "unauthorized"})
    )
    from signoz_mcp.server import list_services

    with pytest.raises(ValueError) as exc_info:
        await list_services(start="-24h")
    message = str(exc_info.value)
    assert "SIGNOZ_API_KEY missing or invalid" in message
    assert "test-api-key" not in message, "the API key value must never reach an exception"


@pytest.mark.asyncio
@respx.mock
async def test_post_surfaces_signoz_error_text():
    """The 400 that the nanoseconds-as-numbers bug produced is this path."""
    respx.post("http://localhost:8080/api/v1/services").mock(
        return_value=Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": (
                        "json: cannot unmarshal number into Go struct field "
                        "GetServicesParams.start of type string"
                    ),
                }
            },
        )
    )
    from signoz_mcp.server import list_services

    with pytest.raises(ValueError) as exc_info:
        await list_services(start="-24h")
    assert "cannot unmarshal number" in str(exc_info.value)
    assert "test-api-key" not in str(exc_info.value)


# ── _client payload shape ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_client_omits_variables_field():
    """v5 query_range payload must not include the v3-era 'variables' top-level field."""
    captured = []

    def capture(request):
        captured.append(request.content)
        return Response(200, json=_v5_trace([]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import search_traces

    await search_traces(service="svc")
    payload = json.loads(captured[0])
    assert "variables" not in payload, "'variables' field was removed in v5"


# ── Fleet-operator surface ────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_passes_the_spec_through():
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=_v5_scalar(["service.name", "__result_0"], [["a", 1]]))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import execute_builder_query

    body = await execute_builder_query(
        signal="traces",
        request_type="scalar",
        start="-24h",
        spec={
            "aggregations": [{"expression": "count()"}],
            "groupBy": [{"name": "service.name"}],
            "limit": 7,
        },
    )

    spec = captured[0]["compositeQuery"]["queries"][0]["spec"]
    assert spec["aggregations"] == [{"expression": "count()"}]
    assert spec["groupBy"] == [{"name": "service.name"}]
    assert spec["limit"] == 7
    # The raw body is returned unparsed — that is what makes it a passthrough
    # rather than a second wrapper.
    assert body["data"]["data"]["results"][0]["columns"] == [
        {"name": "service.name"},
        {"name": "__result_0"},
    ]


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_still_validates_the_filter_expression():
    """An escape hatch from the TOOL SHAPES, not from input validation."""
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_scalar([], []))
    )
    from signoz_mcp.server import execute_builder_query

    with pytest.raises(ValueError, match="Invalid filter expression"):
        await execute_builder_query(
            signal="traces",
            request_type="scalar",
            spec={"filter": {"expression": "service.name = `backtick`"}},
        )


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_cannot_override_the_envelope():
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=_v5_scalar([], []))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import execute_builder_query

    await execute_builder_query(
        signal="traces",
        request_type="scalar",
        spec={"name": "Z", "signal": "logs", "disabled": True, "limit": 1},
    )

    spec = captured[0]["compositeQuery"]["queries"][0]["spec"]
    assert spec["name"] == "A", "caller must not rename the query"
    assert spec["signal"] == "traces", "caller must not redirect the signal via spec"
    assert spec["disabled"] is False


@pytest.mark.asyncio
async def test_execute_builder_query_rejects_a_bad_signal():
    from signoz_mcp.server import execute_builder_query

    with pytest.raises(ValueError):
        await execute_builder_query(signal="profiles", request_type="scalar", spec={})


@pytest.mark.asyncio
@respx.mock
async def test_fleet_health_shapes_three_aggregations_into_one_row():
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(
            200,
            json=_v5_scalar(
                ["service.name", "__result_0", "__result_1", "__result_2"],
                [
                    ["busy", 1000, 2_500_000.0, 25],
                    ["quiet", 10, 500_000.0, 0],
                ],
            ),
        )

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import fleet_health

    rows = await fleet_health(start="-24h")

    # ONE request, so every column comes from the same scan over the same spans.
    assert len(captured) == 1, "fleet_health must not fan out into several queries"
    spec = captured[0]["compositeQuery"]["queries"][0]["spec"]
    assert [a["expression"] for a in spec["aggregations"]] == [
        "count()",
        "p95(duration_nano)",
        "countIf(has_error = true)",
    ]

    assert rows[0] == {
        "service": "busy",
        "calls": 1000,
        "errors": 25,
        "error_rate": 0.025,
        "p95_nano": 2_500_000.0,
        "p95_ms": 2.5,
    }
    assert rows[1]["error_rate"] == 0.0
    assert [r["service"] for r in rows] == ["busy", "quiet"], "busiest first"


@pytest.mark.asyncio
@respx.mock
async def test_fleet_health_does_not_divide_by_zero():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(
            200,
            json=_v5_scalar(
                ["service.name", "__result_0", "__result_1", "__result_2"],
                [["ghost", 0, 0, 0]],
            ),
        )
    )
    from signoz_mcp.server import fleet_health

    assert (await fleet_health())[0]["error_rate"] == 0.0


@pytest.mark.asyncio
@respx.mock
async def test_compare_windows_computes_deltas_across_two_queries():
    bodies = [
        _v5_scalar(["service.name", "__result_0"], [["steady", 100], ["gone", 50]]),
        _v5_scalar(["service.name", "__result_0"], [["steady", 150], ["new", 20]]),
    ]
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=bodies[len(captured) - 1])

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import compare_windows

    rows = await compare_windows(window_a="-48h", window_b="-24h")
    by_service = {r["service.name"]: r for r in rows}

    assert by_service["steady"]["delta"] == 50
    assert by_service["steady"]["pct_change"] == 50.0

    # A group that VANISHED is the interesting row, not one to drop for a tidy join.
    assert by_service["gone"]["after"] == 0
    assert by_service["gone"]["delta"] == -50

    # A NEW group has no percentage change. Reporting 0 or infinity would be a
    # fabricated number, so it is None.
    assert by_service["new"]["before"] == 0
    assert by_service["new"]["pct_change"] is None

    assert [abs(r["delta"]) for r in rows] == sorted(
        (abs(r["delta"]) for r in rows), reverse=True
    ), "largest absolute change first"

    # The baseline window ends where the recent window begins.
    assert captured[0]["end"] == captured[1]["start"]


@pytest.mark.asyncio
async def test_compare_windows_rejects_reversed_windows():
    from signoz_mcp.server import compare_windows

    with pytest.raises(ValueError, match="must be earlier than"):
        await compare_windows(window_a="-24h", window_b="-48h")


@pytest.mark.asyncio
@respx.mock
async def test_compare_windows_groups_by_span_name_without_new_code():
    """Plan item 4: per-span-name is already reachable through group_by."""
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=_v5_scalar(["service.name", "name", "__result_0"], []))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import compare_windows

    await compare_windows(window_a="-48h", window_b="-24h", group_by="service.name,name")

    spec = captured[0]["compositeQuery"]["queries"][0]["spec"]
    assert spec["groupBy"] == [{"name": "service.name"}, {"name": "name"}]


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_validates_every_free_form_string():
    """Not just `filter`. An aggregation expression is the same kind of DSL string,
    and validating one but not the other turns an escape hatch into a bypass."""
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_scalar([], []))
    )
    from signoz_mcp.server import execute_builder_query

    with pytest.raises(ValueError, match="Invalid filter expression"):
        await execute_builder_query(
            signal="traces",
            request_type="scalar",
            spec={"aggregations": [{"expression": "count() `injected`"}]},
        )

    with pytest.raises(ValueError, match="Invalid groupBy field"):
        await execute_builder_query(
            signal="traces",
            request_type="scalar",
            spec={"groupBy": [{"name": "service.name; DROP"}]},
        )

    # order keys take the filter-expression allowlist, not the field-name one —
    # `_build_order` puts the aggregation expression there. So the rejection message
    # is the filter one, and `count()` must still be accepted (asserted below).
    with pytest.raises(ValueError, match="Invalid filter expression"):
        await execute_builder_query(
            signal="traces",
            request_type="scalar",
            spec={"order": [{"key": {"name": "count() `bad`"}, "direction": "desc"}]},
        )


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_accepts_a_well_formed_spec():
    """The other side: validation must not reject the documented example."""
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_scalar(["service.name", "__result_0"], [["a", 1]]))
    )
    from signoz_mcp.server import execute_builder_query

    body = await execute_builder_query(
        signal="traces",
        request_type="scalar",
        start="-168h",
        spec={
            "aggregations": [{"expression": "count()"}],
            "groupBy": [{"name": "service.name"}],
            "order": [{"key": {"name": "count()"}, "direction": "desc"}],
            "limit": 1000,
        },
    )
    assert body["data"]["data"]["results"][0]["data"] == [["a", 1]]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "spec,match",
    [
        ({"having": {"expression": "count() > `x`"}}, "Invalid filter expression"),
        (
            {"secondaryAggregations": [{"expression": "count() `bad`"}]},
            "Invalid filter expression",
        ),
        (
            {"secondaryAggregations": [{"expression": "count()", "groupBy": [{"name": "a;b"}]}]},
            "Invalid secondaryAggregations groupBy field",
        ),
        ({"selectFields": [{"name": "service name!"}]}, "Invalid selectFields field"),
    ],
)
async def test_execute_builder_query_validates_the_measured_field_set(spec, match):
    """These four were reachable and unvalidated until they were MEASURED.

    Enumerating only the fields this file's wrapper tools emit gives a narrower set
    than the v5 API accepts. Each spec below was confirmed to return 200 from live
    SigNoz v0.118.0, i.e. it really does reach the backend — so a guard that skipped
    it would have been a gap, not a no-op.
    """
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_scalar([], []))
    )
    from signoz_mcp.server import execute_builder_query

    with pytest.raises(ValueError, match=match):
        await execute_builder_query(signal="traces", request_type="scalar", spec=spec)


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_accepts_the_measured_fields_when_well_formed():
    """The other direction — the guard must not reject valid uses of those fields.

    This is the assertion that caught the over-tight first attempt at order-key
    validation; a validation change tested only by what it rejects cannot detect
    over-tightening, because every negative case still passes.
    """
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=_v5_scalar([], []))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import execute_builder_query

    await execute_builder_query(
        signal="traces",
        request_type="scalar",
        spec={
            "aggregations": [{"expression": "count()"}],
            "having": {"expression": "count() > 1"},
            "secondaryAggregations": [
                {"expression": "p95(duration_nano)", "groupBy": [{"name": "name"}]}
            ],
            "selectFields": [{"name": "service.name"}],
            "groupBy": [{"name": "service.name"}],
            "order": [{"key": {"name": "count()"}, "direction": "desc"}],
        },
    )

    spec = captured[0]["compositeQuery"]["queries"][0]["spec"]
    assert spec["having"] == {"expression": "count() > 1"}
    assert spec["selectFields"] == [{"name": "service.name"}]
    assert spec["secondaryAggregations"][0]["expression"] == "p95(duration_nano)"


@pytest.mark.asyncio
@respx.mock
async def test_compare_windows_does_not_fabricate_a_disappearance_when_truncated():
    """A group below a truncated window's cut is UNKNOWN, not gone.

    Each window is queried independently with the same limit. Before this was
    handled, a group that merely ranked below `limit` in one window was joined
    against a default of 0 and reported as a vanished service with a -100% change —
    a fabricated finding in the one tool whose entire job is saying what changed.
    """
    # limit=2, so the tool asks for 3 to detect truncation. `before` returns 3 rows
    # (truncated); `after` returns 2 (complete).
    bodies = [
        _v5_scalar(
            ["service.name", "__result_0"],
            [["big", 500], ["mid", 300], ["small", 100]],
        ),
        _v5_scalar(["service.name", "__result_0"], [["big", 400], ["mid", 350]]),
    ]
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=bodies[len(captured) - 1])

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import compare_windows

    rows = await compare_windows(window_a="-48h", window_b="-24h", limit=2)

    # Over-fetch: limit + 1, so "returned exactly limit" is never ambiguous.
    assert captured[0]["compositeQuery"]["queries"][0]["spec"]["limit"] == 3

    by_service = {r["service.name"]: r for r in rows}

    # `small` was trimmed from the truncated `before` side and is genuinely absent
    # from `after`. It must NOT be reported as 100 -> 0.
    assert "small" not in by_service, (
        "a group trimmed from the truncated side leaked into the result"
    )

    # The complete side still yields real numbers.
    assert by_service["big"]["before"] == 500
    assert by_service["big"]["after"] == 400
    assert by_service["big"]["delta"] == -100
    assert by_service["mid"]["delta"] == 50


@pytest.mark.asyncio
@respx.mock
async def test_compare_windows_reports_unknown_rather_than_zero_on_a_truncated_side():
    """A group present only in the COMPLETE window, with the other side truncated."""
    bodies = [
        # before: truncated (3 rows for limit=2), and does not contain `newcomer`
        _v5_scalar(
            ["service.name", "__result_0"],
            [["big", 500], ["mid", 300], ["small", 100]],
        ),
        # after: complete (2 rows), contains `newcomer`
        _v5_scalar(["service.name", "__result_0"], [["big", 400], ["newcomer", 250]]),
    ]
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=bodies[len(captured) - 1])

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import compare_windows

    rows = await compare_windows(window_a="-48h", window_b="-24h", limit=2)
    by_service = {r["service.name"]: r for r in rows}

    # `newcomer` is absent from a TRUNCATED before-window. It may be new, or it may
    # simply have ranked below the cut. The honest answer is "unknown", not 0.
    n = by_service["newcomer"]
    assert n["before"] is None, "a truncated side's absence must not be reported as 0"
    assert n["after"] == 250
    assert n["delta"] is None, "no delta can be computed against an unmeasured value"
    assert n["pct_change"] is None

    # Unknown deltas sort last — they cannot be ranked against measured ones.
    assert rows[-1]["service.name"] == "newcomer"
    assert by_service["big"]["delta"] == -100


@pytest.mark.asyncio
@respx.mock
async def test_compare_windows_uses_zero_when_the_window_was_not_truncated():
    """The other direction: a real disappearance must still be reported as one.

    If the guard treated every absence as unknown it would destroy the tool's main
    use case, so this pins that an UNtruncated window still yields 0 and a real delta.
    """
    bodies = [
        _v5_scalar(["service.name", "__result_0"], [["steady", 100], ["gone", 50]]),
        _v5_scalar(["service.name", "__result_0"], [["steady", 150]]),
    ]
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=bodies[len(captured) - 1])

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import compare_windows

    rows = await compare_windows(window_a="-48h", window_b="-24h", limit=100)
    by_service = {r["service.name"]: r for r in rows}

    assert by_service["gone"]["after"] == 0, "neither window was truncated"
    assert by_service["gone"]["delta"] == -50
    assert by_service["gone"]["pct_change"] == -100.0


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_clamps_limit_and_offset():
    """Security audit F-01 — the passthrough forwarded numeric bounds unchanged.

    Every other tool here clamps `limit` before building its spec. This one copied
    the caller's dict straight through, so the file's limit discipline applied
    everywhere EXCEPT the escape hatch — and "the allowlists still apply" is this
    tool's whole justification. The gap survived the expression/field-name review
    pass because `limit` is not a string.
    """
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=_v5_scalar([], []))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import _MAX_LIMIT_AGG, execute_builder_query

    await execute_builder_query(
        signal="traces",
        request_type="scalar",
        spec={"limit": 10_000_000, "offset": -5},
    )

    spec = captured[0]["compositeQuery"]["queries"][0]["spec"]
    assert spec["limit"] == _MAX_LIMIT_AGG
    assert spec["offset"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_leaves_a_reasonable_limit_alone():
    """The accept side — clamping must not rewrite a value that was already fine."""
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=_v5_scalar([], []))

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)
    from signoz_mcp.server import execute_builder_query

    await execute_builder_query(
        signal="traces", request_type="scalar", spec={"limit": 250, "offset": 10}
    )
    spec = captured[0]["compositeQuery"]["queries"][0]["spec"]
    assert spec["limit"] == 250
    assert spec["offset"] == 10


@pytest.mark.asyncio
@respx.mock
async def test_execute_builder_query_rejects_a_non_numeric_limit():
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_scalar([], []))
    )
    from signoz_mcp.server import execute_builder_query

    with pytest.raises(ValueError, match="must be an integer"):
        await execute_builder_query(signal="traces", request_type="scalar", spec={"limit": "all"})


@pytest.mark.asyncio
@respx.mock
async def test_compare_windows_can_still_detect_truncation_at_the_maximum_limit():
    """The over-fetch must remain REACHABLE at the ceiling — asserted behaviourally.

    `fetch_limit = min(limit + 1, _MAX_LIMIT_AGG)` collapses to `limit` when the
    caller asks for the maximum, so at most `limit` rows come back and
    `len(out) > limit` can never be true. The guard is silently dead at exactly the
    limit where a window is most likely to BE truncated, and the fabricated
    disappearance it prevents comes straight back.

    NOTE ON WHAT THIS ASSERTS. An earlier version of this test checked the requested
    `limit` in the outgoing spec — which is `_MAX_LIMIT_AGG` under BOTH the broken
    and the fixed code, so it passed either way and proved nothing. The observable
    difference is not what is asked for, it is whether a missing group on a
    truncated side comes back as `None` (unknown) or `0` (fabricated).
    """
    from signoz_mcp.server import _MAX_LIMIT_AGG, compare_windows

    # A full page from the `before` window: enough rows that the over-fetch slot is
    # what decides whether truncation is visible at all.
    full_page = [[f"svc-{i:05d}", 1000 - (i % 997)] for i in range(_MAX_LIMIT_AGG)]
    bodies = [
        _v5_scalar(["service.name", "__result_0"], full_page),
        # `after` is small and complete, and contains a group the big page does not.
        _v5_scalar(["service.name", "__result_0"], [["newcomer", 250]]),
    ]
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return Response(200, json=bodies[len(captured) - 1])

    respx.post("http://localhost:8080/api/v5/query_range").mock(side_effect=capture)

    rows = await compare_windows(window_a="-48h", window_b="-24h", limit=_MAX_LIMIT_AGG)
    newcomer = next(r for r in rows if r["service.name"] == "newcomer")

    assert newcomer["before"] is None, (
        "the `before` window returned a full page and must be treated as TRUNCATED, "
        "so `newcomer`'s absence from it is unknown rather than zero. Getting 0 here "
        "means the over-fetch slot was lost and truncation is undetectable at the "
        "ceiling."
    )
    assert newcomer["delta"] is None


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("bad", [1.5, "250", True, None, [1]])
async def test_execute_builder_query_rejects_non_int_limit_rather_than_coercing(bad):
    """`int()` accepts 1.5, "250" and True — silently CHANGING them.

    The first version of this clamp used `int(...)`, so it rewrote values its own
    error message claimed it required to be integers. bool is called out because
    `isinstance(True, int)` is True in Python, so `{"limit": True}` would otherwise
    become 1.
    """
    respx.post("http://localhost:8080/api/v5/query_range").mock(
        return_value=Response(200, json=_v5_scalar([], []))
    )
    from signoz_mcp.server import execute_builder_query

    with pytest.raises(ValueError, match="must be an integer"):
        await execute_builder_query(signal="traces", request_type="scalar", spec={"limit": bad})
