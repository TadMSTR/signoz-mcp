"""signoz-mcp — FastMCP server for SigNoz observability queries.

Read-only access to SigNoz services, traces, logs, metrics, and alert rules.
Gives agents direct query access without requiring a Grafana or SigNoz UI session.

Tools:
  list_services      — Services seen in a time window, with their RED metrics
  search_traces      — Search traces by free-form filter + shortcut params
  aggregate_traces   — Aggregate traces (count/p99/avg/...) grouped by field(s)
  get_trace_details  — Full span list for one trace ID
  tail_logs          — Recent logs filtered by severity
  search_logs        — Search logs by free-form filter + shortcut params
  aggregate_logs     — Aggregate logs (count/...) grouped by field(s)
  query_metric       — Named metric time series with optional label filter
  list_metrics       — Search/list ingested metric names + metadata
  get_field_keys     — Discover filterable field keys for a signal
  get_field_values   — Discover values for a specific field key
  list_alert_rules   — Alert rules + firing state
  get_health         — Connectivity check

Fleet-operator surface:
  execute_builder_query — Raw Query Builder v5 passthrough (the escape hatch)
  fleet_health          — Per-service calls / error rate / p95, in one query
  compare_windows       — Per-group delta between two windows

Configuration:
  SIGNOZ_URL              — SigNoz base URL (default: http://localhost:8080)
  SIGNOZ_API_KEY          — Service Account token (required)
  SIGNOZ_QUERY_VERSION    — query_range API path version (default: v5; only v5 accepted)

API compatibility:
  This server targets the SigNoz v5 query_range API used by v0.118. The v5
  time_series envelope nests results under
  data.data.results[].aggregations[].series[] (labels as a list of
  {"key": {"name": ...}, "value": ...}, values as {"timestamp": ..., "value": ...});
  the scalar envelope uses results[].columns + results[].data (a column-aligned
  table). Both are handled by the parsing helpers below. Metric and field
  listings use the v2/v1 REST endpoints.
"""

from __future__ import annotations

import functools
import inspect
import re
import time
from collections.abc import Callable
from typing import Any

import structlog
from fastmcp import FastMCP

from signoz_mcp import _client as client
from signoz_mcp import telemetry
from signoz_mcp.hooks import run_after_hooks, run_before_hooks

_log = structlog.get_logger("signoz-mcp")

_SERVICE_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_METRIC_NAME_RE = re.compile(r"^[a-zA-Z0-9._:/-]+$")
_FIELD_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
# Discovery search substrings are sent as URL query params (httpx URL-encodes
# them), not spliced into a filter expression — but kept on a conservative
# allowlist to bound the injection surface.
_SEARCH_TEXT_RE = re.compile(r"^[a-zA-Z0-9._:/ -]*$")

# Filter-expression allowlist for SigNoz's structured query DSL. Permits identifiers
# with dots/dashes/colons/slashes (service.name = 'scoped-mcp-developer', body CONTAINS
# '/api/v5'), comparison + logical operators, string literals, IN-lists, and grouping.
# Deliberately EXCLUDES ; ` \ and control characters. The expression is JSON-encoded
# before transport (no context breakout) and SigNoz compiles the DSL to ClickHouse
# server-side (not raw SQL passthrough); this allowlist is defense-in-depth on a
# read-only API. See docs — expansion beyond the v3-era label allowlist is intentional
# and was flagged to the security agent (free-form filter surface).
_FILTER_EXPR_RE = re.compile(r"^[A-Za-z0-9_.:/@%+\-\s='\"<>!()\[\],]*$")
_MAX_FILTER_LEN = 1000
# Retained name for the historical LOW-01 metric label_filter control (now unified
# onto the filter-expression allowlist).
_LABEL_FILTER_RE = _FILTER_EXPR_RE
_MAX_LABEL_FILTER_LEN = _MAX_FILTER_LEN

_MAX_LIMIT_RAW = 500
_MAX_LIMIT_AGG = 10_000

_DURATION_RE = re.compile(r"^-?(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhdw])$", re.IGNORECASE)
_UNIT_MS: dict[str, float] = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}

_ALLOWED_SIGNALS = frozenset({"metrics", "traces", "logs"})
_ALLOWED_SEVERITIES = frozenset({"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"})
# Operation/span names (e.g. "tools/call system-ops_run_command", "GET /api/v5") — allow
# spaces/slashes/colons but NOT quotes, so the value cannot break out of the
# `name = '<operation>'` string literal it is interpolated into (audit LOW, 2026-07-19).
_OPERATION_RE = re.compile(r"^[A-Za-z0-9._:/ -]+$")
# Documented allowlists for the SigNoz field discovery params (audit INFO, 2026-07-19).
_ALLOWED_FIELD_CONTEXTS = frozenset(
    {"resource", "attribute", "tag", "scope", "log", "span", "metric", "body"}
)
_ALLOWED_FIELD_DATA_TYPES = frozenset(
    {
        "string",
        "bool",
        "int64",
        "float64",
        "number",
        "[]string",
        "[]bool",
        "[]int64",
        "[]float64",
        "[]number",
    }
)
_ALLOWED_AGGREGATIONS = frozenset(
    {
        "count",
        "count_distinct",
        "avg",
        "sum",
        "min",
        "max",
        "p50",
        "p75",
        "p90",
        "p95",
        "p99",
        "rate",
    }
)
# count() and rate() take no field argument.
_NO_FIELD_AGGREGATIONS = frozenset({"count", "rate"})
_ALLOWED_REQUEST_TYPES = frozenset({"scalar", "time_series"})

mcp = FastMCP(
    name="signoz",
    instructions=(
        "SigNoz MCP server. Read-only access to observability data on forge. "
        "Use list_services to see all services. "
        "Use search_traces / aggregate_traces / get_trace_details for trace investigation. "
        "Use tail_logs / search_logs / aggregate_logs for log analysis. "
        "Use query_metric / list_metrics for metrics. "
        "Use get_field_keys / get_field_values to discover filterable fields before "
        "building a filter expression. "
        "Use list_alert_rules to check firing alerts, get_health for connectivity. "
        "All tools are read-only — SigNoz write endpoints are never exposed."
    ),
)

# Optional telemetry (OTLP spans+metrics, InfluxDB3, NATS) — no-op unless env-configured.
telemetry.init()


def instrument(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool coroutine with the pre/post hook chain and telemetry.

    Around every tool call this: runs the registered *before* hooks (which may mutate the
    kwargs), opens a telemetry span + records call/error/latency, runs the tool, then runs
    the registered *after* hooks (which may transform the result). Hook exceptions
    propagate — hooks are not fire-and-forget.

    The wrapped callable keeps ``fn``'s signature (via ``__signature__``) so FastMCP still
    derives the correct tool schema.
    """
    tool_name = fn.__name__
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        call_kwargs = dict(bound.arguments)
        call_kwargs = await run_before_hooks(tool_name, call_kwargs)
        async with telemetry.record_tool_call(tool_name):
            result = await fn(**call_kwargs)
        return await run_after_hooks(tool_name, result)

    wrapper.__signature__ = sig  # type: ignore[attr-defined]
    return wrapper


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register ``fn`` as an instrumented MCP tool. Use as ``@tool`` (no parentheses)."""
    return mcp.tool()(instrument(fn))


# ── Time helpers ──────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_duration_ms(expr: str) -> int:
    """Parse a relative duration like '-1h', '-30m', '7d' into milliseconds from now."""
    m = _DURATION_RE.match(expr.strip())
    if not m:
        raise ValueError(f"Cannot parse duration: {expr!r} — use e.g. '-1h', '-30m', '-7d'")
    value = float(m.group("value"))
    unit = m.group("unit").lower()
    delta_ms = int(value * _UNIT_MS[unit])
    return _now_ms() - delta_ms


def _parse_time_ms(expr: str) -> int:
    """Parse a time expression and return epoch milliseconds.

    Accepts:
      - Relative durations: '-1h', '-30m', '-7d'
      - Keyword: 'now'
    """
    expr = expr.strip()
    if expr.lower() == "now":
        return _now_ms()
    return _parse_duration_ms(expr)


# ── Input validation ──────────────────────────────────────────────────────────


def _validate_service(service: str) -> str:
    if not _SERVICE_RE.match(service):
        raise ValueError(
            f"Invalid service name {service!r}: "
            "only alphanumeric, dash, underscore, and dot allowed"
        )
    return service


def _validate_severity(severity: str) -> str:
    sev = severity.upper()
    if sev not in _ALLOWED_SEVERITIES:
        raise ValueError(
            f"Invalid severity {severity!r}: must be one of {sorted(_ALLOWED_SEVERITIES)}"
        )
    return sev


def _validate_signal(signal: str) -> str:
    sig = signal.lower()
    if sig not in _ALLOWED_SIGNALS:
        raise ValueError(f"Invalid signal {signal!r}: must be one of {sorted(_ALLOWED_SIGNALS)}")
    return sig


def _validate_field_name(name: str, what: str = "field name") -> str:
    if not _FIELD_NAME_RE.match(name):
        raise ValueError(
            f"Invalid {what} {name!r}: only alphanumeric, dot, underscore, dash allowed"
        )
    return name


def _validate_operation(operation: str) -> str:
    """Validate a span/operation name before interpolating it into a filter literal.

    Stricter than the free-form filter allowlist: no quotes, so the value cannot break
    out of the `name = '<operation>'` string literal.
    """
    if not _OPERATION_RE.match(operation):
        raise ValueError(
            f"Invalid operation {operation!r}: only alphanumeric, dot, underscore, "
            "colon, slash, dash, and spaces allowed"
        )
    return operation


def _validate_filter_expr(expr: str) -> str:
    """Validate a free-form SigNoz filter expression against the allowlist.

    Security-relevant: this is the free-form injection surface into the SigNoz
    query API. The expression is JSON-encoded before transport and SigNoz parses
    it as a structured DSL server-side; this allowlist is defense-in-depth.
    """
    if len(expr) > _MAX_FILTER_LEN:
        raise ValueError(f"filter too long: max {_MAX_FILTER_LEN} chars")
    if not _FILTER_EXPR_RE.match(expr):
        raise ValueError(
            "Invalid filter expression: contains disallowed characters. Allowed: "
            "letters, digits, _ . : / @ % + - and = ' \" < > ! ( ) [ ] , plus whitespace"
        )
    return expr


def _build_agg_expression(aggregation: str, aggregate_on: str) -> str:
    """Build a SigNoz aggregation expression like 'count()' or 'p99(duration_nano)'."""
    agg = aggregation.lower()
    if agg not in _ALLOWED_AGGREGATIONS:
        raise ValueError(
            f"Invalid aggregation {aggregation!r}: must be one of {sorted(_ALLOWED_AGGREGATIONS)}"
        )
    if agg in _NO_FIELD_AGGREGATIONS:
        return f"{agg}()"
    if not aggregate_on:
        raise ValueError(f"aggregation {agg!r} requires aggregate_on (a field name)")
    _validate_field_name(aggregate_on, "aggregate_on")
    return f"{agg}({aggregate_on})"


def _build_group_by(group_by: str) -> list[dict]:
    """Parse a comma-separated field list into v5 groupBy keys."""
    keys = []
    for raw in group_by.split(","):
        name = raw.strip()
        if not name:
            continue
        _validate_field_name(name, "group_by field")
        keys.append({"name": name})
    return keys


def _build_order(order_by: str, default_expr: str) -> list[dict]:
    """Parse an 'order_by' string ('<field> <asc|desc>') into a v5 order clause.

    Empty order_by defaults to the aggregation expression, descending.
    """
    if not order_by.strip():
        return [{"key": {"name": default_expr}, "direction": "desc"}]
    parts = order_by.split()
    field = parts[0]
    direction = parts[1].lower() if len(parts) > 1 else "desc"
    if direction not in {"asc", "desc"}:
        raise ValueError("order_by direction must be 'asc' or 'desc'")
    # The field may be an aggregation expression like 'p99(duration_nano)'; validate
    # it through the filter-expression allowlist which permits parens.
    _validate_filter_expr(field)
    return [{"key": {"name": field}, "direction": direction}]


# ── v5 query_range response parsing ───────────────────────────────────────────


def _v5_results(body: dict) -> list[dict]:
    """Extract the results list from a v5 query_range response envelope."""
    return body.get("data", {}).get("data", {}).get("results", []) or []


def _labels_to_dict(labels: object) -> dict:
    """Convert a v5 series labels list to a flat {name: value} dict.

    v5 returns labels as [{"key": {"name": "serviceName"}, "value": "x"}, ...].
    A plain dict (older/mocked shape) is passed through unchanged.
    """
    if isinstance(labels, dict):
        return labels
    out: dict = {}
    for lab in labels or []:
        if not isinstance(lab, dict):
            continue
        key = lab.get("key")
        name = key.get("name") if isinstance(key, dict) else key
        if isinstance(name, str):
            out[name] = lab.get("value")
    return out


def _point_value(point: object) -> float | None:
    """Extract the numeric value from a v5 series point.

    v5 points are {"timestamp": ..., "value": ...}. A legacy [ts, val] pair is
    also accepted so we degrade gracefully on shape drift.
    """
    if isinstance(point, dict):
        val = point.get("value")
    elif isinstance(point, (list, tuple)) and len(point) >= 2:
        val = point[1]
    else:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _sum_series_values(values: object) -> float:
    return sum(v for v in (_point_value(p) for p in values or []) if v is not None)


def _iter_agg_series(body: dict):
    """Yield (labels_dict, values_list) for every series of every aggregation."""
    for r in _v5_results(body):
        for agg in r.get("aggregations") or []:
            for series in agg.get("series") or []:
                yield _labels_to_dict(series.get("labels")), series.get("values") or []


def _extract_rows(body: dict) -> list[dict]:
    """Flatten trace/raw rows, unwrapping the per-row {'data': {...}} envelope."""
    rows: list[dict] = []
    for r in _v5_results(body):
        for row in r.get("rows") or []:
            if isinstance(row, dict) and isinstance(row.get("data"), dict):
                rows.append(row["data"])
            else:
                rows.append(row)
    return rows


def _parse_scalar_rows(body: dict) -> list[dict]:
    """Parse a v5 scalar response (columns + column-aligned data table) into dicts.

    Scalar results look like:
      {"columns": [{"name": "service.name"}, {"name": "__result_0"}],
       "data": [["svc-a", 1835], ["svc-b", 932]]}
    → [{"service.name": "svc-a", "__result_0": 1835}, ...]
    """
    rows: list[dict] = []
    for r in _v5_results(body):
        columns = [c.get("name") for c in r.get("columns") or []]
        for row in r.get("data") or []:
            if isinstance(row, (list, tuple)):
                rows.append(dict(zip(columns, row, strict=False)))
            elif isinstance(row, dict):
                rows.append(row)
    return rows


def _parse_aggregate(body: dict, request_type: str) -> list[dict]:
    """Shape an aggregate response by request type (scalar table vs time series)."""
    if request_type == "scalar":
        return _parse_scalar_rows(body)
    return [{"labels": labels, "values": values} for labels, values in _iter_agg_series(body)]


# ── Empty-log-store distinguisher (vikunja#926) ───────────────────────────────

# Contexts that only exist once something has actually been INGESTED. SigNoz always
# reports its built-in log schema — `body`, `severity_number`, `scope_name` and so on,
# at fieldContext 'log' and 'scope' — whether or not a single log line has ever
# arrived. Resource and attribute keys are different: they are derived from the data.
#
# MEASURED ON FORGE, 2026-09-20, against SigNoz v0.118.0:
#
#   signal    total keys    contexts                                  resource|attribute
#   logs               8    {log: 6, scope: 2}                                         0
#   traces           181    {attribute: 136, resource: 21, span: 23, scope: 2}       156
#   metrics           64    {attribute: 47, resource: 21, metric: 1}                  63
#
# This matters because the obvious form of this check does NOT work. The plan for this
# build proposed testing whether the logs signal has any field keys at all and treating
# empty as the signal — but the payload is not empty on an empty store, it holds those
# eight built-ins. A guard written that way would never once have fired.
_DERIVED_FIELD_CONTEXTS = frozenset({"resource", "attribute"})


async def _logs_signal_has_ingested_data() -> bool:
    """True if the logs signal carries any field key derived from real data."""
    data = await client.get("/api/v1/fields/keys", params={"signal": "logs"})
    payload = data.get("data", {}) if isinstance(data, dict) else {}
    keys = payload.get("keys") or {}
    return any(
        defn.get("fieldContext") in _DERIVED_FIELD_CONTEXTS
        for defns in keys.values()
        if isinstance(defns, list)
        for defn in defns
        if isinstance(defn, dict)
    )


async def _raise_if_logs_signal_is_empty() -> None:
    """Distinguish "no matching logs" from "this backend holds no logs at all".

    Every log tool here returns `[]` for both, and an agent cannot tell them apart.
    That ambiguity is what produced vikunja#909: parse errors were diagnosed against
    a table that had nothing in it, so no filter expression could ever have worked.

    Called only on the EMPTY path, so this costs one extra request exactly when the
    result would otherwise have been uninformative — never on a successful query.
    """
    if await _logs_signal_has_ingested_data():
        return  # genuinely no matching rows; the caller's empty list is the answer
    raise ValueError(
        "SigNoz holds no log data at all — this is not an empty result for your "
        "query. The logs signal reports only SigNoz's built-in schema keys and no "
        "resource or attribute keys, meaning nothing has ever been ingested. "
        "Nothing on forge currently exports OTLP logs and the collector has no "
        "filelog receiver: see vikunja#926. Narrowing or widening this query will "
        "not help; trace-side tools (search_traces, aggregate_traces) are unaffected."
    )


# ── Tools ─────────────────────────────────────────────────────────────────────


# Fields returned per service by POST /api/v1/services, in the order they are
# presented. `dataWarning` is deliberately dropped: its only member is
# `topLevelOps`, which includes SigNoz's synthetic "overflow_operation" entry and
# is noise in a fleet listing. Everything here is SigNoz's own field name —
# renaming them would invent a mapping this server would then have to keep true.
_SERVICE_FIELDS = (
    "serviceName",
    "p99",
    "avgDuration",
    "numCalls",
    "callRate",
    "numErrors",
    "errorRate",
    "num4XX",
    "fourXXRate",
)


@tool
async def list_services(start: str = "-1h", end: str = "now") -> list[dict]:
    """List services seen in SigNoz within a time window, with their RED metrics.

    Args:
        start: Window start — relative duration ('-24h', '-7d') or 'now'.
        end:   Window end — same format. Defaults to 'now'.

    Returns:
        One dict per service, each carrying SigNoz's own field names:
        `serviceName`, `p99` and `avgDuration` (NANOSECONDS — verified against
        `p99(duration_nano)` from the trace aggregate, same order of magnitude),
        `numCalls`, `callRate`, `numErrors`, `errorRate`, `num4XX`, `fourXXRate`.

        Note that `p99`/`avgDuration` are computed over each service's TOP-LEVEL
        operations, not every span, so they do not match
        `aggregate_traces(p99(duration_nano))` exactly. Measured 2026-09-20 the
        two agree within ~7% for most services and diverge up to 2x for services
        with deep span trees. Use `aggregate_traces` when you need all spans.
    """
    # vikunja#322. This used GET /api/v1/services/list, which takes NO time range
    # and applies its own short implicit window. Measured live on 2026-09-20
    # against SigNoz v0.118.0: that endpoint returned 16 services regardless of
    # window, while this POST form returned 21 at 24h and 25 at 7d — set-identical
    # to aggregate_traces(count, group_by=service.name) at BOTH windows. Nine
    # services were missing over 7 days, including scoped-mcp-doc-health and
    # memsearch-summarize.
    #
    # `start` and `end` MUST be JSON STRINGS of nanoseconds. Passing numbers
    # returns 400 "json: cannot unmarshal number into Go struct field
    # GetServicesParams.start of type string" — confirmed live, do not
    # rediscover it.
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    data = await client.post(
        "/api/v1/services",
        {
            "start": str(start_ms * 1_000_000),
            "end": str(end_ms * 1_000_000),
            "tags": [],
        },
    )
    if not isinstance(data, list):
        return []
    return [
        {k: svc[k] for k in _SERVICE_FIELDS if k in svc} for svc in data if isinstance(svc, dict)
    ]


@tool
async def search_traces(
    filter: str = "",
    service: str = "",
    operation: str = "",
    has_error: bool = False,
    min_duration_ms: int = 0,
    max_duration_ms: int = 0,
    start: str = "-1h",
    end: str = "now",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Search traces by a free-form filter expression plus shortcut params.

    Args:
        filter:          Free-form SigNoz filter expression, e.g.
                         "service.name = 'frontend' AND http.status_code = 500".
                         Combined with the shortcut params below via AND.
        service:         Shortcut for "service.name = '<service>'".
        operation:       Shortcut for "name = '<operation>'" (span/operation name).
        has_error:       Shortcut for "has_error = true".
        min_duration_ms: Shortcut for "duration_nano >= <ms * 1e6>". 0 = no filter.
        max_duration_ms: Shortcut for "duration_nano <= <ms * 1e6>". 0 = no filter.
        start:           Start time, e.g. '-1h'. end: end time (default 'now').
        limit:           Max traces to return (max 500). offset: pagination offset.

    Returns:
        List of trace dicts (trace_id, name, service.name, duration_nano, span_count, ...).
    """
    parts: list[str] = []
    if filter:
        parts.append(_validate_filter_expr(filter))
    if service:
        _validate_service(service)
        parts.append(f"service.name = '{service}'")
    if operation:
        _validate_operation(operation)
        parts.append(f"name = '{operation}'")
    if has_error:
        parts.append("has_error = true")
    if min_duration_ms > 0:
        parts.append(f"duration_nano >= {int(min_duration_ms) * 1_000_000}")
    if max_duration_ms > 0:
        parts.append(f"duration_nano <= {int(max_duration_ms) * 1_000_000}")

    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(max(limit, 1), _MAX_LIMIT_RAW)

    spec: dict = {
        "order": [{"key": {"name": "timestamp"}, "direction": "desc"}],
        "limit": limit,
        "offset": max(offset, 0),
    }
    if parts:
        spec["filter"] = {"expression": " AND ".join(parts)}

    body = await client.query("traces", "trace", spec, start_ms, end_ms)
    return _extract_rows(body)[:limit]


@tool
async def aggregate_traces(
    aggregation: str,
    aggregate_on: str = "",
    group_by: str = "",
    filter: str = "",
    service: str = "",
    operation: str = "",
    error: bool = False,
    min_duration_ms: int = 0,
    max_duration_ms: int = 0,
    order_by: str = "",
    limit: int = 100,
    start: str = "-1h",
    end: str = "now",
    request_type: str = "scalar",
    step_interval: int = 60,
) -> list[dict]:
    """Aggregate traces with a function grouped by field(s).

    Replaces the removed count_errors tool, e.g.
    aggregate_traces(aggregation="count", filter="has_error = true",
                     group_by="service.name").

    Args:
        aggregation:   One of count, count_distinct, avg, sum, min, max,
                       p50/p75/p90/p95/p99, rate. count/rate take no aggregate_on.
        aggregate_on:  Field to aggregate, e.g. 'duration_nano' (required unless
                       aggregation is count/rate).
        group_by:      Comma-separated field names, e.g. 'service.name,name'.
        filter:        Free-form filter expression (AND-combined with shortcuts).
        service/operation/error/min_duration_ms/max_duration_ms: shortcut filters.
        order_by:      '<field> <asc|desc>'. Default: the aggregation expr, desc.
        limit:         Max groups (max 10000).
        request_type:  'scalar' (single value per group) or 'time_series'.
        step_interval: Step in seconds (time_series only).

    Returns:
        scalar → list of {<group field>: value, __result_0: number} dicts.
        time_series → list of {labels, values} series dicts.
    """
    agg_expr = _build_agg_expression(aggregation, aggregate_on)
    req_type = request_type.lower()
    if req_type not in _ALLOWED_REQUEST_TYPES:
        raise ValueError(f"request_type must be one of {sorted(_ALLOWED_REQUEST_TYPES)}")

    parts: list[str] = []
    if filter:
        parts.append(_validate_filter_expr(filter))
    if service:
        _validate_service(service)
        parts.append(f"service.name = '{service}'")
    if operation:
        _validate_operation(operation)
        parts.append(f"name = '{operation}'")
    if error:
        parts.append("has_error = true")
    if min_duration_ms > 0:
        parts.append(f"duration_nano >= {int(min_duration_ms) * 1_000_000}")
    if max_duration_ms > 0:
        parts.append(f"duration_nano <= {int(max_duration_ms) * 1_000_000}")

    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(max(limit, 1), _MAX_LIMIT_AGG)

    spec: dict = {
        "aggregations": [{"expression": agg_expr}],
        "order": _build_order(order_by, agg_expr),
        "limit": limit,
    }
    group_keys = _build_group_by(group_by)
    if group_keys:
        spec["groupBy"] = group_keys
    if parts:
        spec["filter"] = {"expression": " AND ".join(parts)}
    if req_type == "time_series":
        spec["stepInterval"] = step_interval

    body = await client.query("traces", req_type, spec, start_ms, end_ms)
    return _parse_aggregate(body, req_type)


@tool
async def get_trace_details(
    trace_id: str,
    start: str = "-6h",
    end: str = "now",
    include_spans: bool = True,
) -> list[dict]:
    """Return the spans (or a one-row summary) for a single trace ID.

    Args:
        trace_id:      Trace ID (hex). Validated before use.
        start:         Start of the search window (default '-6h'). end: default 'now'.
        include_spans: If True (default), return every span in the trace
                       (span_id, name, service.name, duration_nano, timestamp, ...).
                       If False, return a single trace-summary row (span_count).

    Returns:
        List of span dicts (include_spans=True) or a one-item summary list.
    """
    if not re.fullmatch(r"[A-Fa-f0-9]{1,64}", trace_id):
        raise ValueError(f"Invalid trace_id {trace_id!r}: expected a hex string (up to 64 chars)")
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)

    spec = {
        "filter": {"expression": f"trace_id = '{trace_id}'"},
        "order": [{"key": {"name": "timestamp"}, "direction": "asc"}],
        "limit": 1000,
    }
    # requestType 'raw' returns individual spans; 'trace' returns a trace summary row.
    request_type = "raw" if include_spans else "trace"
    body = await client.query("traces", request_type, spec, start_ms, end_ms)
    return _extract_rows(body)


@tool
async def tail_logs(
    severity: str = "ERROR",
    start: str = "-1h",
    end: str = "now",
    limit: int = 50,
) -> list[dict]:
    """Return the most recent logs at a given severity, newest first.

    Args:
        severity: Log severity level, e.g. 'ERROR', 'WARN', 'INFO'. Case-insensitive.
        start:    Start time, e.g. '-1h'. end: end time (default 'now').
        limit:    Max log lines to return (max 500).

    Returns:
        List of log dicts with timestamp, severity_text, body, and resource fields.

    To scope by service, use `search_logs(filter=...)` — but see vikunja#926 first:
    no service on forge currently exports OTLP logs, so the log store is empty and
    no service-scoping filter can be verified against real data today.
    """
    # vikunja#927. This took a REQUIRED `service` argument, validated it at the top
    # of the body, and then never referenced it again — the spec below filtered on
    # severity_text alone. Callers got a plausible-looking answer that had silently
    # ignored the one thing they asked to narrow by.
    #
    # The parameter is DROPPED rather than wired into the filter. Scoping it properly
    # means choosing a filter key, and the two log tools in this file already
    # disagree about which key that is (see the note in search_logs). With the log
    # store empty, either choice is a guess that cannot be tested — and this tool is
    # already the result of one guess that got written into a docstring as though it
    # were a design decision. Picking the key belongs to the build that fixes #926,
    # where get_field_keys(signal="logs") will finally return data-derived keys.
    sev = _validate_severity(severity)
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(limit, _MAX_LIMIT_RAW)

    spec = {
        "filter": {"expression": f"severity_text = '{sev}'"},
        "order": [{"key": {"name": "timestamp"}, "direction": "desc"}],
        "limit": limit,
    }
    body = await client.query("logs", "raw", spec, start_ms, end_ms)
    rows = _extract_rows(body)[:limit]
    if not rows:
        await _raise_if_logs_signal_is_empty()
    return rows


@tool
async def search_logs(
    filter: str = "",
    service: str = "",
    severity: str = "",
    search_text: str = "",
    start: str = "-1h",
    end: str = "now",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Search logs by a free-form filter expression plus shortcut params.

    Args:
        filter:      Free-form SigNoz filter expression, AND-combined with shortcuts.
        service:     Shortcut for "service.name = '<service>'". UNVERIFIED — see the
                     note in the body about vikunja#926; this may return a parse
                     error. Prefer narrowing by time + severity until logs exist.
        severity:    Shortcut for "severity_text = '<SEVERITY>'".
        search_text: Shortcut for "body CONTAINS '<text>'" (log body substring).
        start:       Start time (default '-1h'). end: end time (default 'now').
        limit:       Max log lines (max 500). offset: pagination offset.

    Returns:
        List of log dicts (timestamp, severity_text, body, resource fields, ...).
    """
    # WHICH KEY NAMES A SERVICE IN THE LOGS SIGNAL IS UNRESOLVED (vikunja#926).
    # This function emits `service.name`. aggregate_logs' docstring recommends
    # `resource.service.name` while its own body also emits `service.name`, so the
    # two tools — and one of them internally — disagree. They cannot all be right.
    #
    # Deliberately NOT resolved here. get_field_keys(signal="logs") returns only
    # SigNoz's built-in schema and no resource keys at all, because nothing has ever
    # been ingested, so picking a key now would be a guess dressed as a decision —
    # which is exactly how vikunja#927 happened one tool over. The correction belongs
    # to the build that fixes ingestion, where the field keys will finally say.
    parts: list[str] = []
    if filter:
        parts.append(_validate_filter_expr(filter))
    if service:
        _validate_service(service)
        parts.append(f"service.name = '{service}'")
    if severity:
        parts.append(f"severity_text = '{_validate_severity(severity)}'")
    if search_text:
        if not _SEARCH_TEXT_RE.match(search_text):
            raise ValueError("Invalid search_text: disallowed characters")
        parts.append(f"body CONTAINS '{search_text}'")

    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(max(limit, 1), _MAX_LIMIT_RAW)

    spec: dict = {
        "order": [{"key": {"name": "timestamp"}, "direction": "desc"}],
        "limit": limit,
        "offset": max(offset, 0),
    }
    if parts:
        spec["filter"] = {"expression": " AND ".join(parts)}

    body = await client.query("logs", "raw", spec, start_ms, end_ms)
    rows = _extract_rows(body)[:limit]
    if not rows:
        await _raise_if_logs_signal_is_empty()
    return rows


@tool
async def aggregate_logs(
    aggregation: str,
    aggregate_on: str = "",
    group_by: str = "",
    filter: str = "",
    service: str = "",
    severity: str = "",
    search_text: str = "",
    order_by: str = "",
    limit: int = 100,
    start: str = "-1h",
    end: str = "now",
    request_type: str = "scalar",
    step_interval: int = 60,
) -> list[dict]:
    """Aggregate logs with a function grouped by field(s).

    Replaces the removed count_log_errors tool, e.g.
    aggregate_logs(aggregation="count", filter="severity_text IN ['ERROR', 'WARN']",
                   group_by="resource.service.name").

    Args:
        aggregation:   One of count, count_distinct, avg, sum, min, max,
                       p50/p75/p90/p95/p99, rate. count/rate take no aggregate_on.
        aggregate_on:  Field to aggregate (required unless count/rate).
        group_by:      Comma-separated field names, e.g. 'resource.service.name'.
        filter:        Free-form filter expression (AND-combined with shortcuts).
        service/severity/search_text: shortcut filters (see search_logs).
        order_by:      '<field> <asc|desc>'. Default: the aggregation expr, desc.
        limit:         Max groups (max 10000).
        request_type:  'scalar' or 'time_series'. step_interval: seconds (time_series).

    Returns:
        scalar → list of {<group field>: value, __result_0: number} dicts.
        time_series → list of {labels, values} series dicts.
    """
    agg_expr = _build_agg_expression(aggregation, aggregate_on)
    req_type = request_type.lower()
    if req_type not in _ALLOWED_REQUEST_TYPES:
        raise ValueError(f"request_type must be one of {sorted(_ALLOWED_REQUEST_TYPES)}")

    parts: list[str] = []
    if filter:
        parts.append(_validate_filter_expr(filter))
    if service:
        _validate_service(service)
        parts.append(f"service.name = '{service}'")
    if severity:
        parts.append(f"severity_text = '{_validate_severity(severity)}'")
    if search_text:
        if not _SEARCH_TEXT_RE.match(search_text):
            raise ValueError("Invalid search_text: disallowed characters")
        parts.append(f"body CONTAINS '{search_text}'")

    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(max(limit, 1), _MAX_LIMIT_AGG)

    spec: dict = {
        "aggregations": [{"expression": agg_expr}],
        "order": _build_order(order_by, agg_expr),
        "limit": limit,
    }
    group_keys = _build_group_by(group_by)
    if group_keys:
        spec["groupBy"] = group_keys
    if parts:
        spec["filter"] = {"expression": " AND ".join(parts)}
    if req_type == "time_series":
        spec["stepInterval"] = step_interval

    body = await client.query("logs", req_type, spec, start_ms, end_ms)
    return _parse_aggregate(body, req_type)


@tool
async def query_metric(
    metric_name: str,
    label_filter: str = "",
    start: str = "-1h",
    end: str = "now",
    step_interval: int = 60,
) -> list[dict]:
    """Query a named metric as a time series with an optional label filter.

    Args:
        metric_name:   Metric name, e.g. 'scoped_mcp.credentials.healthy'.
        label_filter:  Optional filter expression, e.g. "state = 'idle'".
        start:         Start time, e.g. '-1h'. end: end time (default 'now').
        step_interval: Aggregation step in seconds.

    Returns:
        List of series dicts, each with 'labels' (dict) and 'values'
        (list of {timestamp, value} points).
    """
    if not _METRIC_NAME_RE.match(metric_name):
        raise ValueError(
            f"Invalid metric name {metric_name!r}: "
            "only alphanumeric, dot, underscore, colon, slash, dash allowed"
        )
    # SECURITY[resolved]: validate label_filter against the filter-expression
    # allowlist before passing to the SigNoz query API. LOW-01 from
    # 2026-05-30/signoz-mcp-deploy-2026-05, unified onto _FILTER_EXPR_RE.
    if label_filter:
        _validate_filter_expr(label_filter)

    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)

    # v5 metrics API: metricName lives inside the aggregation object, not at spec top level.
    # timeAggregation and spaceAggregation replace the v3 expression-based format.
    aggregation: dict = {
        "metricName": metric_name,
        "timeAggregation": "avg",
        "spaceAggregation": "avg",
    }
    spec: dict = {
        "stepInterval": step_interval,
        "aggregations": [aggregation],
    }
    if label_filter:
        spec["filter"] = {"expression": label_filter}

    body = await client.query("metrics", "time_series", spec, start_ms, end_ms)
    rows = [{"labels": labels, "values": values} for labels, values in _iter_agg_series(body)]
    return rows[:200]


@tool
async def list_metrics(
    search_text: str = "",
    start: str = "-1h",
    end: str = "now",
    limit: int = 50,
    source: str = "",
) -> list[dict]:
    """Search and list metric names ingested in SigNoz, with metadata.

    Sources metric names from live time-series data via the v2 metrics endpoint
    (the /api/v1/metricsNames endpoint used before v0.118 was removed).

    Args:
        search_text: Filter metric names by substring, e.g. 'cpu'. Empty = all.
        start:       Start of the discovery window, e.g. '-1h'. end: default 'now'.
        limit:       Max metrics to return (max 500).
        source:      Optional data-source filter. Use 'meter' for Cost Meter
                     usage metrics; omit for the default metrics store.

    Returns:
        List of metric dicts with metricName, description, type, unit,
        temporality, and isMonotonic.
    """
    if search_text and not _SEARCH_TEXT_RE.match(search_text):
        raise ValueError(
            "Invalid search_text: only alphanumeric, dot, underscore, colon, "
            "slash, dash, and spaces allowed"
        )
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(max(limit, 1), _MAX_LIMIT_RAW)

    params: dict = {"start": start_ms, "end": end_ms, "limit": limit}
    if search_text:
        params["searchText"] = search_text
    if source:
        params["source"] = source

    data = await client.get("/api/v2/metrics", params=params)
    metrics = data.get("data", {}).get("metrics", [])
    if not isinstance(metrics, list):
        return []
    return metrics[:limit]


@tool
async def get_field_keys(
    signal: str,
    search_text: str = "",
    metric_name: str = "",
    field_context: str = "",
    field_data_type: str = "",
) -> dict:
    """Discover available field keys for a signal (for building filters).

    Args:
        signal:          One of 'metrics', 'traces', 'logs'.
        search_text:     Filter field names by substring (optional).
        metric_name:     Scope keys to a metric (only when signal='metrics').
        field_context:   Restrict to one context: 'resource', 'attribute',
                         'scope', 'log'/'span'/'metric', 'body' (optional).
        field_data_type: Restrict to a data type: 'string', 'bool', 'int64',
                         'float64', 'number' (optional).

    Returns:
        SigNoz field-keys payload: {'keys': {<name>: [<key metadata>, ...]}, ...}.
    """
    _validate_signal(signal)
    if search_text and not _SEARCH_TEXT_RE.match(search_text):
        raise ValueError("Invalid search_text: disallowed characters")
    if metric_name and not _METRIC_NAME_RE.match(metric_name):
        raise ValueError("Invalid metric_name: disallowed characters")
    if field_context and field_context.lower() not in _ALLOWED_FIELD_CONTEXTS:
        raise ValueError(
            f"Invalid field_context {field_context!r}: must be one of "
            f"{sorted(_ALLOWED_FIELD_CONTEXTS)}"
        )
    if field_data_type and field_data_type not in _ALLOWED_FIELD_DATA_TYPES:
        raise ValueError(
            f"Invalid field_data_type {field_data_type!r}: must be one of "
            f"{sorted(_ALLOWED_FIELD_DATA_TYPES)}"
        )

    params: dict = {"signal": signal.lower()}
    if search_text:
        params["searchText"] = search_text
    if metric_name:
        params["metricName"] = metric_name
    if field_context:
        params["fieldContext"] = field_context
    if field_data_type:
        params["fieldDataType"] = field_data_type

    data = await client.get("/api/v1/fields/keys", params=params)
    return data.get("data", {}) if isinstance(data, dict) else {}


@tool
async def get_field_values(
    signal: str,
    name: str,
    search_text: str = "",
    metric_name: str = "",
    field_context: str = "",
) -> dict:
    """Discover possible values for a specific field key.

    Args:
        signal:        One of 'metrics', 'traces', 'logs'.
        name:          Field key to fetch values for, e.g. 'service.name'.
        search_text:   Filter returned values by substring (optional).
        metric_name:   Scope values to a metric (only when signal='metrics').
        field_context: Disambiguate context when the key exists in more than one
                       (optional): 'resource', 'attribute', 'scope', etc.

    Returns:
        SigNoz field-values payload: {'values': {'stringValues': [...], ...}}.
    """
    _validate_signal(signal)
    if not name:
        raise ValueError("name is required")
    _validate_field_name(name)
    if search_text and not _SEARCH_TEXT_RE.match(search_text):
        raise ValueError("Invalid search_text: disallowed characters")
    if metric_name and not _METRIC_NAME_RE.match(metric_name):
        raise ValueError("Invalid metric_name: disallowed characters")
    if field_context and field_context.lower() not in _ALLOWED_FIELD_CONTEXTS:
        raise ValueError(
            f"Invalid field_context {field_context!r}: must be one of "
            f"{sorted(_ALLOWED_FIELD_CONTEXTS)}"
        )

    params: dict = {"signal": signal.lower(), "name": name}
    if search_text:
        params["searchText"] = search_text
    if metric_name:
        params["metricName"] = metric_name
    if field_context:
        params["fieldContext"] = field_context

    data = await client.get("/api/v1/fields/values", params=params)
    return data.get("data", {}) if isinstance(data, dict) else {}


@tool
async def list_alert_rules() -> list[dict]:
    """List all alert rules and their current firing state.

    Returns:
        List of alert rule dicts with name, state, and condition details.
    """
    data = await client.get("/api/v1/rules")
    rules = data if isinstance(data, list) else data.get("data", {}).get("rules", [])
    return rules[:200]


@tool
async def get_health() -> dict:
    """Check SigNoz connectivity.

    Returns:
        Health status dict from SigNoz /api/v1/health.
    """
    return await client.get("/api/v1/health")


# All registered tool names — used to wire the audit-log before-hook across the surface.
_TOOL_NAMES = (
    "list_services",
    "search_traces",
    "aggregate_traces",
    "get_trace_details",
    "tail_logs",
    "search_logs",
    "aggregate_logs",
    "query_metric",
    "list_metrics",
    "get_field_keys",
    "get_field_values",
    "list_alert_rules",
    "get_health",
)


# ── Fleet-operator surface ────────────────────────────────────────────────────


def _scalar_results(body: dict) -> list[dict]:
    """Parse a multi-aggregation scalar response into dicts keyed by column name."""
    return _parse_scalar_rows(body)


@tool
async def execute_builder_query(
    signal: str,
    request_type: str,
    spec: dict,
    start: str = "-1h",
    end: str = "now",
) -> dict:
    """Run a raw SigNoz Query Builder v5 spec. The escape hatch.

    Every other tool here is a wrapper, and vikunja#322 is what a wrong wrapper
    costs: `list_services` called an endpoint that silently under-reported, and
    callers had no way through. This is the way through — if a tool's shape is
    wrong for your question, build the spec yourself rather than working around it.

    Args:
        signal:       'traces', 'logs' or 'metrics'.
        request_type: 'scalar' or 'time_series'.
        spec:         The builder_query spec body — `aggregations`, `groupBy`,
                      `filter`, `order`, `limit`, `offset`. `name`, `signal` and
                      `disabled` are supplied for you. `limit` is clamped to
                      1..10,000 and `offset` to >= 0, the same ceilings the
                      wrapper tools apply — the escape hatch is from this server's
                      tool SHAPES, not from its limits.
        start/end:    Window, e.g. '-24h' / 'now'.

    Returns:
        The parsed SigNoz response body, unmodified. Parsing it is the caller's
        job — that is the point of a passthrough. `_parse_scalar_rows`-shaped
        output is available from `aggregate_traces` if you want it done for you.

    Example — reproduce the 7d service list from first principles:
        execute_builder_query(
            signal="traces", request_type="scalar", start="-168h",
            spec={"aggregations": [{"expression": "count()"}],
                  "groupBy": [{"name": "service.name"}], "limit": 1000},
        )
    """
    sig = _validate_signal(signal)
    req_type = request_type.lower()
    if req_type not in _ALLOWED_REQUEST_TYPES:
        raise ValueError(f"request_type must be one of {sorted(_ALLOWED_REQUEST_TYPES)}")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a dict")

    # THE ALLOWLISTS STILL APPLY. A passthrough is an escape hatch from this
    # server's TOOL SHAPES, not from its input validation — that distinction is the
    # whole reason this is safe to add.
    #
    # THE SET OF POSITIONS BELOW WAS MEASURED, NOT INFERRED FROM THE WRAPPERS.
    # Enumerating only the fields the wrapper tools emit gives a NARROWER set than
    # the API accepts, and the gap is silent. Probed live against SigNoz v0.118.0 on
    # 2026-09-20 by POSTing each candidate field and reading the status:
    #
    #   having.expression                     200  <- accepted, free-form expression
    #   secondaryAggregations[].expression    200  <- accepted, free-form expression
    #   secondaryAggregations[].groupBy[]     200  <- accepted, field names
    #   selectFields[].name                   200  <- accepted, field names
    #   functions[]                           200  <- accepted, but {name, args} only
    #   filter as a bare string               400  <- rejected by SigNoz itself
    #
    # None of those is emitted by any tool in this file, so all four were reachable
    # and unvalidated. SigNoz does check `having` server-side (the backtick probe
    # came back 400 "Invalid references"), but that is SigNoz's defence, not this
    # server's, and relying on it would make our guard depend on a backend version.
    safe_spec = dict(spec)

    def _check_expression_holder(obj: object) -> None:
        """Validate an {"expression": "..."} node, wherever it appears."""
        if isinstance(obj, dict) and isinstance(obj.get("expression"), str):
            _validate_filter_expr(obj["expression"])

    def _check_field_names(entries: object, what: str) -> None:
        """Validate a list of {"name": "..."} field-name nodes."""
        if not isinstance(entries, list):
            return
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                _validate_field_name(entry["name"], what)

    # Free-form expression positions.
    filt = safe_spec.get("filter")
    if isinstance(filt, dict) and isinstance(filt.get("expression"), str):
        safe_spec["filter"] = {**filt, "expression": _validate_filter_expr(filt["expression"])}
    elif isinstance(filt, str):
        # SigNoz rejects a bare string here (400), but normalising it is kinder than
        # forwarding something we know will fail — and it must still be validated.
        safe_spec["filter"] = {"expression": _validate_filter_expr(filt)}

    _check_expression_holder(safe_spec.get("having"))

    aggs = safe_spec.get("aggregations")
    if isinstance(aggs, list):
        for agg in aggs:
            _check_expression_holder(agg)

    secondary = safe_spec.get("secondaryAggregations")
    if isinstance(secondary, list):
        for agg in secondary:
            _check_expression_holder(agg)
            if isinstance(agg, dict):
                _check_field_names(agg.get("groupBy"), "secondaryAggregations groupBy field")

    # Plain field-name positions get the STRICTER check.
    _check_field_names(safe_spec.get("groupBy"), "groupBy field")
    _check_field_names(safe_spec.get("selectFields"), "selectFields field")

    # order keys are NOT plain field names — `_build_order` deliberately sends the
    # aggregation expression there ({"key": {"name": "count()"}}), and fleet_health
    # above does the same. So they get the filter-expression allowlist, which permits
    # parens, exactly as `_build_order` does. Using `_validate_field_name` here looks
    # tighter and would reject this tool's own documented example.
    order = safe_spec.get("order")
    if isinstance(order, list):
        for entry in order:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("key")
            if isinstance(nested, dict) and isinstance(nested.get("name"), str):
                _validate_filter_expr(nested["name"])
            elif isinstance(entry.get("name"), str):
                _validate_filter_expr(entry["name"])

    # NUMERIC BOUNDS APPLY TOO. Security audit F-01
    # (signoz-mcp-standard-defects-2026-09): every other tool in this file clamps
    # `limit` before building its spec, and this one forwarded whatever the caller
    # put in the dict. That was the same asymmetry the expression/field-name
    # validation above exists to close — just in a position that is not a string, so
    # the pass that found those did not look at it.
    #
    # _MAX_LIMIT_AGG rather than _MAX_LIMIT_RAW because request_type is validated
    # against {"scalar", "time_series"} above; the raw path is not reachable here.
    #
    # Read-only backend, so the concern is cost and response size, not data access —
    # but "the allowlists still apply" is this tool's entire justification for
    # existing, and a ceiling that applies everywhere except the escape hatch is not
    # a ceiling.
    if "limit" in safe_spec:
        try:
            safe_spec["limit"] = min(max(int(safe_spec["limit"]), 1), _MAX_LIMIT_AGG)
        except (TypeError, ValueError):
            raise ValueError("spec['limit'] must be an integer") from None
    if "offset" in safe_spec:
        try:
            safe_spec["offset"] = max(int(safe_spec["offset"]), 0)
        except (TypeError, ValueError):
            raise ValueError("spec['offset'] must be an integer") from None

    # These are set by _build_query_payload; a caller overriding them would be
    # reaching past the passthrough into the envelope.
    for reserved in ("name", "signal", "disabled"):
        safe_spec.pop(reserved, None)

    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    return await client.query(sig, req_type, safe_spec, start_ms, end_ms)


@tool
async def fleet_health(start: str = "-1h", end: str = "now", limit: int = 1000) -> list[dict]:
    """Per-service call count, error rate and p95 latency — the first question.

    Composing this from aggregate_traces takes three or four calls. This is ONE,
    which also means every column comes from the same scan over the same spans:
    `count()`, `p95(duration_nano)` and `countIf(has_error = true)` are requested
    as three aggregations on a single query.

    That consistency is deliberate. `list_services` also returns per-service RED
    metrics, but its `p99`/`avgDuration` cover each service's TOP-LEVEL operations
    only — mixing the two sources would put two different scopes in adjacent
    columns of the same row.

    Args:
        start/end: Window, e.g. '-24h' / 'now'.
        limit:     Max services (max 10000).

    Returns:
        One dict per service, busiest first:
        `service`, `calls`, `errors`, `error_rate` (0.0-1.0, `errors/calls`),
        `p95_nano` (raw, as SigNoz returns it) and `p95_ms` (the same value / 1e6,
        rounded to 3dp — a stated derivation, not a separate measurement).
    """
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    spec = {
        "aggregations": [
            {"expression": "count()"},
            {"expression": "p95(duration_nano)"},
            {"expression": "countIf(has_error = true)"},
        ],
        "groupBy": _build_group_by("service.name"),
        "order": [{"key": {"name": "count()"}, "direction": "desc"}],
        "limit": min(max(limit, 1), _MAX_LIMIT_AGG),
    }
    body = await client.query("traces", "scalar", spec, start_ms, end_ms)

    rows: list[dict] = []
    for row in _scalar_results(body):
        service = row.get("service.name")
        if not service:
            continue
        calls = row.get("__result_0") or 0
        p95_nano = row.get("__result_1") or 0
        errors = row.get("__result_2") or 0
        rows.append(
            {
                "service": service,
                "calls": calls,
                "errors": errors,
                # Guarded rather than assumed non-zero: a group can only exist if it
                # has spans, but a future filter could make that untrue silently.
                "error_rate": round(errors / calls, 6) if calls else 0.0,
                "p95_nano": p95_nano,
                "p95_ms": round(p95_nano / 1_000_000, 3),
            }
        )
    rows.sort(key=lambda r: -r["calls"])
    return rows


@tool
async def compare_windows(
    window_a: str,
    window_b: str,
    aggregation: str = "count",
    aggregate_on: str = "",
    group_by: str = "service.name",
    filter: str = "",
    limit: int = 1000,
) -> list[dict]:
    """Per-group delta between two time windows — "what changed since the deploy".

    A raw count is a stock, not a flow. The operator question is almost never "how
    many errors are there" but "are there more than before", and answering that by
    eye from two separate tool calls is where the mistake gets made.

    Args:
        window_a:     The EARLIER/baseline window, e.g. '-48h'. Runs from
                      window_a to window_b.
        window_b:     The boundary between the two windows, e.g. '-24h'. The
                      recent window runs from window_b to now.
        aggregation:  count, count_distinct, avg, sum, min, max, p50-p99, rate.
        aggregate_on: Field to aggregate (required unless count/rate).
        group_by:     Comma-separated fields. Defaults to 'service.name'; use
                      'name' for per-span-name, or 'service.name,name' for both.
        filter:       Free-form filter expression applied to BOTH windows.
        limit:        Max groups per window.

    Returns:
        One dict per group, largest absolute change first:
        `<group field(s)>`, `before`, `after`, `delta` (after - before), and
        `pct_change` (None when `before` is 0 — a new group has no percentage
        change, and reporting one as 0 or infinity would be a fabricated number).

        Groups present in only one window ARE included, with 0 for the side they
        are missing from. Those are usually the interesting rows — a service that
        stopped reporting is exactly what this is for — and dropping them to make
        the join tidy would hide the finding.

        **`before`/`after` can be `None`, and that is not the same as 0.** Each
        window is capped at `limit` groups independently, so a group can be absent
        from one window because it ranked below that window's cut rather than
        because it was gone. When that window hit the cap, the missing side is
        reported as `None` — unknown — and `delta`/`pct_change` are `None` too,
        rather than a difference computed against a zero nobody measured. Rows with
        an unknown delta sort last. Raise `limit` to resolve them.
    """
    agg_expr = _build_agg_expression(aggregation, aggregate_on)
    group_keys = _build_group_by(group_by)
    if not group_keys:
        raise ValueError("group_by must name at least one field")
    group_names = [k["name"] for k in group_keys]
    limit = min(max(limit, 1), _MAX_LIMIT_AGG)

    # OVER-FETCH BY ONE, so truncation is a FACT rather than a suspicion.
    #
    # Each window is queried independently with the same `limit`, ordered by the
    # aggregation descending. If a window has more groups than `limit`, the ones below
    # the cut are simply absent from that window's response — and the naive join below
    # used to read an absence as a zero, which reports a group that merely ranked low
    # as having VANISHED (or, on the other side, as brand new). That is a fabricated
    # finding in the one tool whose whole job is telling you what changed.
    #
    # Asking for `limit + 1` makes the test exact: getting `limit + 1` rows back proves
    # there was at least one more, whereas "returned exactly `limit`" is ambiguous —
    # a window with exactly `limit` groups is complete and indistinguishable from a
    # truncated one. The extra row is then discarded, so `limit` keeps meaning what it
    # says.
    fetch_limit = min(limit + 1, _MAX_LIMIT_AGG)
    spec: dict = {
        "aggregations": [{"expression": agg_expr}],
        "groupBy": group_keys,
        "order": _build_order("", agg_expr),
        "limit": fetch_limit,
    }
    if filter:
        spec["filter"] = {"expression": _validate_filter_expr(filter)}

    a_start, a_end = _parse_time_ms(window_a), _parse_time_ms(window_b)
    b_start, b_end = _parse_time_ms(window_b), _parse_time_ms("now")
    if a_start >= a_end:
        raise ValueError(
            f"window_a ({window_a}) must be earlier than window_b ({window_b}) — "
            "the baseline window runs from window_a to window_b"
        )

    before_body = await client.query("traces", "scalar", spec, a_start, a_end)
    after_body = await client.query("traces", "scalar", spec, b_start, b_end)

    def keyed(body: dict) -> tuple[dict[tuple, float], bool]:
        """Group values in response order, plus whether the window was truncated."""
        out: dict[tuple, float] = {}
        for row in _scalar_results(body):
            key = tuple(row.get(n) for n in group_names)
            if any(k is None for k in key):
                continue
            out[key] = row.get("__result_0") or 0
        truncated = len(out) > limit
        if truncated:
            # Rows arrive ordered by the aggregation descending, so the tail is the
            # part below the cut. Drop it to honour `limit`.
            out = dict(list(out.items())[:limit])
        return out, truncated

    before, before_truncated = keyed(before_body)
    after, after_truncated = keyed(after_body)

    def side(values: dict[tuple, float], key: tuple, truncated: bool) -> float | None:
        """A group's value, or None when its absence cannot be distinguished from
        having fallen below a truncated window's limit."""
        if key in values:
            return values[key]
        return None if truncated else 0

    rows: list[dict] = []
    for key in before.keys() | after.keys():
        b = side(before, key, before_truncated)
        a = side(after, key, after_truncated)
        row = dict(zip(group_names, key, strict=False))
        row["before"] = b
        row["after"] = a
        # None propagates deliberately. "I don't know what this was" must not be
        # arithmetic'd into a delta that looks measured.
        row["delta"] = None if b is None or a is None else a - b
        row["pct_change"] = (
            round((a - b) / b * 100, 2) if b not in (None, 0) and a is not None else None
        )
        rows.append(row)

    # Unknown deltas sort last: they cannot be ranked against measured ones, and
    # putting them first would give an unmeasurable row top billing.
    rows.sort(key=lambda r: (r["delta"] is None, -abs(r["delta"] or 0)))
    return rows


def main() -> None:
    from .contrib.audit_log import register_audit_log
    from .observability import configure_logging

    configure_logging()
    # Audit-log every query (who/what/args-hash) via the existing structlog logger.
    register_audit_log(_TOOL_NAMES)
    mcp.run()


if __name__ == "__main__":
    main()
