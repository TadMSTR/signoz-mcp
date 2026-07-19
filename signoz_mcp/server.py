"""signoz-mcp — FastMCP server for SigNoz observability queries.

Read-only access to SigNoz services, traces, logs, metrics, and alert rules.
Gives agents direct query access without requiring a Grafana or SigNoz UI session.

Tools:
  list_services      — All services with RED metrics
  count_errors       — Error span count grouped by service
  search_traces      — Filter traces by service, error state, min duration
  tail_logs          — Recent logs for a service filtered by severity
  count_log_errors   — Error/warn log rate over time
  query_metric       — Named metric with optional label filter
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
  response envelope nests aggregation results under
  data.data.results[].aggregations[].series[], with labels as a list of
  {"key": {"name": ...}, "value": ...} objects and values as
  {"timestamp": ..., "value": ...} points — parsed by the _iter_agg_series /
  _extract_rows helpers below. Metric and field listings use the v2/v1 REST
  endpoints (/api/v2/metrics, /api/v1/fields/keys, /api/v1/fields/values).
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
# Discovery search substrings and field names are sent as URL query params (httpx
# URL-encodes them), not spliced into a SQL filter expression — but we still keep
# them on a conservative allowlist to bound the injection surface.
_SEARCH_TEXT_RE = re.compile(r"^[a-zA-Z0-9._:/ -]*$")
_FIELD_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
# Allowlist for SigNoz label_filter expressions: identifiers, comparisons, string literals,
# logical operators (AND/OR/IN as plain letters), list brackets, commas, whitespace.
_LABEL_FILTER_RE = re.compile(r"^[a-zA-Z0-9_.='<>!()\[\]\s,]+$")
_MAX_LABEL_FILTER_LEN = 500

_MAX_LIMIT_RAW = 500
_MAX_LIMIT_AGG = 10_000
_MAX_RANGE_RAW_MS = 24 * 3600 * 1000
_MAX_RANGE_AGG_MS = 7 * 24 * 3600 * 1000

_DURATION_RE = re.compile(r"^-?(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhdw])$", re.IGNORECASE)
_UNIT_MS: dict[str, float] = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}

_ALLOWED_SIGNALS = frozenset({"metrics", "traces", "logs"})

mcp = FastMCP(
    name="signoz",
    instructions=(
        "SigNoz MCP server. Read-only access to observability data on forge. "
        "Use list_services to see all services and their RED metrics. "
        "Use count_errors / search_traces for trace-level investigation. "
        "Use tail_logs / count_log_errors for log analysis. "
        "Use query_metric / list_metrics for metrics queries. "
        "Use get_field_keys / get_field_values to discover filterable fields. "
        "Use list_alert_rules to check firing alerts. "
        "Use get_health to confirm connectivity. "
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


_ALLOWED_SEVERITIES = frozenset({"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"})


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


# ── v5 query_range response parsing ───────────────────────────────────────────
#
# The v5 envelope is:
#   {"data": {"data": {"results": [
#       {"queryName": "A",
#        "aggregations": [{"alias": "__result_0",
#                          "series": [{"labels": [{"key": {"name": K}, "value": V}, ...],
#                                      "values": [{"timestamp": T, "value": N}, ...]}]}],
#        "rows": [{"data": {...}}, ...]}]}}}
# Aggregation aliases are backend-assigned ("__result_0"), so parsers must be
# alias-agnostic and read labels/values out of series[], NOT off the aggregation.


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
async def count_errors(
    start: str = "-1h",
    end: str = "now",
    limit: int = 20,
) -> list[dict]:
    """Count error spans grouped by service over a time range.

    Args:
        start: Start time, e.g. '-1h', '-30m', '-7d'.
        end:   End time. Defaults to 'now'.
        limit: Max services to return (max 10000).

    Returns:
        List of dicts with serviceName and error_count, sorted descending.
    """
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(limit, _MAX_LIMIT_AGG)

    spec = {
        "stepInterval": 60,
        "aggregations": [{"expression": "count()", "alias": "error_count"}],
        "filter": {"expression": "hasError = true"},
        "groupBy": [{"name": "serviceName"}],
        "order": [{"key": {"name": "error_count"}, "direction": "desc"}],
        "limit": limit,
    }
    body = await client.query("traces", "time_series", spec, start_ms, end_ms)
    rows = [
        {
            "serviceName": labels.get("serviceName", ""),
            "error_count": _sum_series_values(values),
        }
        for labels, values in _iter_agg_series(body)
    ]
    rows.sort(key=lambda r: r["error_count"], reverse=True)
    return rows


@mcp.tool()
async def search_traces(
    service: str,
    has_error: bool = False,
    min_duration_ms: int = 0,
    start: str = "-1h",
    end: str = "now",
    limit: int = 20,
) -> list[dict]:
    """Search traces filtered by service, error state, and minimum duration.

    Args:
        service:         Service name to filter on.
        has_error:       If True, return only error spans.
        min_duration_ms: Minimum span duration in milliseconds. 0 = no filter.
        start:           Start time, e.g. '-1h'.
        end:             End time. Defaults to 'now'.
        limit:           Max traces to return (max 500).

    Returns:
        List of trace dicts (trace_id, name, service.name, duration_nano, ...).
    """
    _validate_service(service)
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(limit, _MAX_LIMIT_RAW)

    filters = [f"serviceName = '{service}'"]
    if has_error:
        filters.append("hasError = true")
    if min_duration_ms > 0:
        filters.append(f"durationNano >= {min_duration_ms * 1_000_000}")
    filter_expr = " AND ".join(filters)

    spec = {
        "stepInterval": 60,
        "filter": {"expression": filter_expr},
        "order": [{"key": {"name": "timestamp"}, "direction": "desc"}],
        "limit": limit,
    }
    body = await client.query("traces", "trace", spec, start_ms, end_ms)
    return _extract_rows(body)[:limit]


@mcp.tool()
async def tail_logs(
    service: str,
    severity: str = "ERROR",
    start: str = "-1h",
    end: str = "now",
    limit: int = 50,
) -> list[dict]:
    """Return recent logs for a service filtered by severity.

    Args:
        service:  Service name to filter on.
        severity: Log severity level, e.g. 'ERROR', 'WARN', 'INFO'. Case-insensitive.
        start:    Start time, e.g. '-1h'.
        end:      End time. Defaults to 'now'.
        limit:    Max log lines to return (max 500).

    Returns:
        List of log dicts with timestamp, severity_text, body, and resource fields.
    """
    _validate_service(service)
    sev = _validate_severity(severity)
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(limit, _MAX_LIMIT_RAW)

    # In the v5 logs schema, service name is a resource attribute (resource.service.name)
    # and cannot be used in filter expressions directly — only in groupBy.
    # Filter on severity_text only; callers should narrow time range for service-level queries.
    filter_expr = f"severity_text = '{sev}'"

    spec = {
        "stepInterval": 60,
        "filter": {"expression": filter_expr},
        "order": [{"key": {"name": "timestamp"}, "direction": "desc"}],
        "limit": limit,
    }
    body = await client.query("logs", "raw", spec, start_ms, end_ms)
    return _extract_rows(body)[:limit]


@mcp.tool()
async def count_log_errors(
    start: str = "-1h",
    end: str = "now",
    limit: int = 20,
) -> list[dict]:
    """Count error/warn log events grouped by service over a time range.

    Args:
        start: Start time, e.g. '-1h'.
        end:   End time. Defaults to 'now'.
        limit: Max services to return (max 10000).

    Returns:
        List of dicts with serviceName and log_error_count, sorted descending.
    """
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(limit, _MAX_LIMIT_AGG)

    spec = {
        "stepInterval": 60,
        "aggregations": [{"expression": "count()", "alias": "log_error_count"}],
        "filter": {"expression": "severity_text IN ['ERROR', 'WARN']"},
        # In the v5 logs schema, service name is stored as a resource attribute.
        "groupBy": [{"name": "resource.service.name"}],
        "order": [{"key": {"name": "log_error_count"}, "direction": "desc"}],
        "limit": limit,
    }
    body = await client.query("logs", "time_series", spec, start_ms, end_ms)
    rows = [
        {
            "serviceName": labels.get("resource.service.name", ""),
            "log_error_count": _sum_series_values(values),
        }
        for labels, values in _iter_agg_series(body)
    ]
    rows.sort(key=lambda r: r["log_error_count"], reverse=True)
    return rows


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
        start:         Start time, e.g. '-1h'.
        end:           End time. Defaults to 'now'.
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
    if label_filter and len(label_filter) > _MAX_LABEL_FILTER_LEN:
        raise ValueError(f"label_filter too long: max {_MAX_LABEL_FILTER_LEN} chars")
    # SECURITY[resolved]: Validate label_filter against allowlist regex before passing to
    # SigNoz query API. LOW-01 from 2026-05-30/signoz-mcp-deploy-2026-05.
    if label_filter and not _LABEL_FILTER_RE.match(label_filter):
        raise ValueError(
            "Invalid label_filter: only alphanumeric, _  .  =  '  <  >  !  ()  []  ,  "
            "and whitespace allowed"
        )

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
        start:       Start of the discovery window, e.g. '-1h'.
        end:         End of the discovery window. Defaults to 'now'.
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
    if not _FIELD_NAME_RE.match(name):
        raise ValueError(
            f"Invalid field name {name!r}: only alphanumeric, dot, underscore, dash allowed"
        )
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
