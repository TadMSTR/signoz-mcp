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
  list_metrics       — All ingested metric names
  list_alert_rules   — Alert rules + firing state
  get_health         — Connectivity check

Configuration:
  SIGNOZ_URL              — SigNoz base URL (default: http://localhost:8080)
  SIGNOZ_API_KEY          — Service Account token (required)
  SIGNOZ_QUERY_VERSION    — API path version: v3 or v5 (default: v3)
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

mcp = FastMCP(
    name="signoz",
    instructions=(
        "SigNoz MCP server. Read-only access to observability data on forge. "
        "Use list_services to see all services and their RED metrics. "
        "Use count_errors / search_traces for trace-level investigation. "
        "Use tail_logs / count_log_errors for log analysis. "
        "Use query_metric / list_metrics for metrics queries. "
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
    results = body.get("data", {}).get("result", [])
    rows = []
    for series in results:
        metric = series.get("metric", {})
        values = series.get("values", [])
        total = sum(float(v) for _, v in values) if values else 0
        rows.append({"serviceName": metric.get("serviceName", ""), "error_count": total})
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
        List of trace dicts with traceID, spanID, serviceName, durationNano, hasError.
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
    return body.get("data", {}).get("result", [])[:limit]


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
        List of log dicts with timestamp, severityText, body, and service fields.
    """
    _validate_service(service)
    sev = _validate_severity(severity)
    start_ms = _parse_time_ms(start)
    end_ms = _parse_time_ms(end)
    limit = min(limit, _MAX_LIMIT_RAW)

    filter_expr = f"serviceName = '{service}' AND severityText = '{sev}'"

    spec = {
        "stepInterval": 60,
        "filter": {"expression": filter_expr},
        "order": [{"key": {"name": "timestamp"}, "direction": "desc"}],
        "limit": limit,
    }
    body = await client.query("logs", "raw", spec, start_ms, end_ms)
    return body.get("data", {}).get("result", [])[:limit]


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
        "filter": {"expression": "severityText IN ['ERROR', 'WARN']"},
        "groupBy": [{"name": "serviceName"}],
        "order": [{"key": {"name": "log_error_count"}, "direction": "desc"}],
        "limit": limit,
    }
    body = await client.query("logs", "time_series", spec, start_ms, end_ms)
    results = body.get("data", {}).get("result", [])
    rows = []
    for series in results:
        metric = series.get("metric", {})
        values = series.get("values", [])
        total = sum(float(v) for _, v in values) if values else 0
        rows.append({"serviceName": metric.get("serviceName", ""), "log_error_count": total})
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
        metric_name:   Metric name, e.g. 'system_cpu_time'.
        label_filter:  Optional filter expression, e.g. 'state = 'idle''.
        start:         Start time, e.g. '-1h'.
        end:           End time. Defaults to 'now'.
        step_interval: Aggregation step in seconds.

    Returns:
        List of time-series dicts with metric labels and values array.
    """
    if not _METRIC_NAME_RE.match(metric_name):
        raise ValueError(f"Invalid metric name {metric_name!r}: only alphanumeric, dot, underscore, colon, slash, dash allowed")
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

    spec: dict = {
        "metricName": metric_name,
        "stepInterval": step_interval,
        "aggregations": [{"expression": "avg(value)", "alias": "avg"}],
    }
    if label_filter:
        spec["filter"] = {"expression": label_filter}

    body = await client.query("metrics", "time_series", spec, start_ms, end_ms)
    return body.get("data", {}).get("result", [])[:200]


@mcp.tool()
async def list_metrics() -> list[str]:
    """List all metric names currently ingested in SigNoz.

    Returns:
        Sorted list of metric name strings.
    """
    data = await client.get("/api/v1/metricsNames")
    names = data if isinstance(data, list) else data.get("data", [])
    return sorted(names)[:500]


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
