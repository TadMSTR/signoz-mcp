"""signoz-mcp — FastMCP server for SigNoz observability queries.

Read-only access to SigNoz services, traces, logs, metrics, and alert rules.
Gives agents direct query access without requiring a Grafana or SigNoz UI session.

Tools:
  list_services      — All service names registered in SigNoz
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

import re
import time

import structlog
from fastmcp import FastMCP

from signoz_mcp import _client as client

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


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_services() -> list[str]:
    """List all service names registered in SigNoz.

    Returns:
        List of service name strings.
    """
    data = await client.get("/api/v1/services/list")
    return data if isinstance(data, list) else []


@mcp.tool()
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
        _validate_filter_expr(operation)
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


@mcp.tool()
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
        _validate_filter_expr(operation)
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


@mcp.tool()
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


@mcp.tool()
async def tail_logs(
    service: str,
    severity: str = "ERROR",
    start: str = "-1h",
    end: str = "now",
    limit: int = 50,
) -> list[dict]:
    """Return recent logs filtered by severity.

    Args:
        service:  Service name (validated; recorded for context — see note).
        severity: Log severity level, e.g. 'ERROR', 'WARN', 'INFO'. Case-insensitive.
        start:    Start time, e.g. '-1h'. end: end time (default 'now').
        limit:    Max log lines to return (max 500).

    Note:
        In forge's v5 log schema, service name is a resource attribute that is not
        reliably filterable in the log filter parser, so this tool filters on
        severity_text only. Use search_logs(filter=...) for richer log filtering.

    Returns:
        List of log dicts with timestamp, severity_text, body, and resource fields.
    """
    _validate_service(service)
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
    return _extract_rows(body)[:limit]


@mcp.tool()
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
        service:     Shortcut for "service.name = '<service>'". Note: forge's log
                     pipeline may not index service.name as filterable — this can
                     return a parse error; prefer narrowing by time + severity.
        severity:    Shortcut for "severity_text = '<SEVERITY>'".
        search_text: Shortcut for "body CONTAINS '<text>'" (log body substring).
        start:       Start time (default '-1h'). end: end time (default 'now').
        limit:       Max log lines (max 500). offset: pagination offset.

    Returns:
        List of log dicts (timestamp, severity_text, body, resource fields, ...).
    """
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
    return _extract_rows(body)[:limit]


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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

    params: dict = {"signal": signal.lower(), "name": name}
    if search_text:
        params["searchText"] = search_text
    if metric_name:
        params["metricName"] = metric_name
    if field_context:
        params["fieldContext"] = field_context

    data = await client.get("/api/v1/fields/values", params=params)
    return data.get("data", {}) if isinstance(data, dict) else {}


@mcp.tool()
async def list_alert_rules() -> list[dict]:
    """List all alert rules and their current firing state.

    Returns:
        List of alert rule dicts with name, state, and condition details.
    """
    data = await client.get("/api/v1/rules")
    rules = data if isinstance(data, list) else data.get("data", {}).get("rules", [])
    return rules[:200]


@mcp.tool()
async def get_health() -> dict:
    """Check SigNoz connectivity.

    Returns:
        Health status dict from SigNoz /api/v1/health.
    """
    return await client.get("/api/v1/health")


def main() -> None:
    from .observability import configure_logging

    configure_logging()
    mcp.run()


if __name__ == "__main__":
    main()
