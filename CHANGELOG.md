# Changelog

## [0.3.0] — 2026-07-19

### Breaking

- **`count_errors` removed** — subsumed by
  `aggregate_traces(aggregation="count", filter="has_error = true", group_by="service.name")`.
- **`count_log_errors` removed** — subsumed by
  `aggregate_logs(aggregation="count", filter="severity_text IN ['ERROR', 'WARN']", group_by="resource.service.name")`.
- **`search_traces` signature changed** — was
  `search_traces(service, has_error, min_duration_ms, start, end, limit)` with a
  required `service`; now `search_traces(filter="", service="", operation="",
  has_error=False, min_duration_ms=0, max_duration_ms=0, start, end, limit=100,
  offset=0)`. All params are optional; `filter` accepts a free-form SigNoz filter
  expression AND-combined with the shortcut params. Default `limit` is now 100
  (was 20). Trace field names in generated filters use the v5-canonical dotted
  form (`service.name`, `has_error`, `duration_nano`).

### Added

- **Telemetry + pre/post-hook layer** (forge MCP standard, ported from vikunja-mcp
  v0.2.0). All off by default; the base install gains zero new required deps.
  - `signoz_mcp/telemetry.py` — per-tool-call OTLP spans + metrics
    (`signoz_mcp.tool.calls`/`.errors`/`.latency`), plus best-effort fire-and-forget
    InfluxDB 3 and NATS sinks. Every backend import is lazy/guarded. New env vars
    (all optional): `SIGNOZ_MCP_INFLUXDB3_URL`/`_TOKEN`/`_DATABASE`,
    `SIGNOZ_MCP_NATS_URL`/`_SUBJECT`, and the existing `OTEL_EXPORTER_OTLP_ENDPOINT`
    (now actually wired to metrics + spans). The `SIGNOZ_MCP_` prefix keeps this
    server's telemetry config separate from the upstream `SIGNOZ_*` connection vars.
  - `signoz_mcp/hooks.py` — `register_before`/`register_after` extension-hook
    registry; before-hooks can mutate/abort a call, after-hooks can transform results.
  - `server.instrument`/`server.tool` wrap every tool as
    `run_before_hooks → span/metric → tool body → run_after_hooks`, preserving each
    tool's signature (`wrapper.__signature__`) so FastMCP's schema introspection is
    unchanged.
  - `signoz_mcp/contrib/audit_log.py` — a read-only audit-log before-hook
    (who/what/args-hash, never raw arg values) registered across all tools at startup.
  - New `telemetry` optional-dependency extra (replaces the narrower `otel` extra):
    `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`, `influxdb3-python`,
    `nats-py`.
- `aggregate_traces(...)` / `aggregate_logs(...)` — generic aggregation tools
  (count/count_distinct/avg/sum/min/max/p50–p99/rate) with `group_by`, free-form
  `filter` + shortcut params, and `request_type` `scalar` (default) or
  `time_series`. Scalar responses are parsed from the v5 columns/data table shape.
- `search_logs(...)` — free-form log search (filter + service/severity/search_text
  shortcuts; `search_text` does `body CONTAINS`).
- `get_trace_details(trace_id, start, end, include_spans=True)` — returns every
  span in a trace (`include_spans=True`, via the `raw` request type) or a one-row
  trace summary (`include_spans=False`, via the `trace` request type).

### Security

- Introduced `_FILTER_EXPR_RE`, an expanded filter-expression allowlist for the
  new free-form `filter` params (and unified `query_metric`'s `label_filter` onto
  it). It now permits `- : / @ %` (real service names have dashes; log-body
  filters have slashes) while still excluding `;` `` ` `` `\\` and control
  characters, with a 1000-char cap. The expression is JSON-encoded before
  transport and SigNoz compiles the DSL to ClickHouse server-side (not raw SQL);
  this allowlist is defense-in-depth on a read-only API. Flagged to the security
  agent as the build's one deliberate injection-surface expansion.
- Audit remediation (2026-07-19, LOW): the `operation` shortcut in `search_traces`
  and `aggregate_traces` is now validated with a strict allowlist (`_validate_operation`,
  no quotes) instead of the permissive filter-expression allowlist, so it cannot break
  out of the `name = '<operation>'` string literal it is interpolated into.
- Audit remediation (2026-07-19, INFO): `get_field_keys`/`get_field_values` now validate
  `field_context` and `field_data_type` against their documented allowlists, matching the
  validation applied to the other discovery params.

### Fixed

- **Query-response parsing was broken against live SigNoz v0.118 (SGNZ-8).** The
  v0.2.0 v3→v5 migration updated the request payloads but never validated the
  response parsing against a live instance — the 100%-mocked test suite encoded an
  assumed response shape that did not match reality. The real v5 envelope nests
  aggregation results under `data.data.results[].aggregations[].series[]`, with
  labels as a list of `{"key": {"name": ...}, "value": ...}` objects and values as
  `{"timestamp": ..., "value": ...}` points, and a backend-assigned aggregation
  `alias` (`__result_0`). As a result:
  - `count_errors` and `count_log_errors` filtered on `alias == "error_count"` /
    `"log_error_count"` (never matched) and read `labels`/`values` off the
    aggregation instead of its `series` — so both **silently returned `[]`** against
    real data. Now fixed and verified live.
  - `query_metric` returned raw nested aggregation objects; it now returns clean
    `{labels: {...}, values: [{timestamp, value}]}` series.
  - Note: the SGNZ-8 report's premise (that `/api/v5/query_range` 404s and the fix
    is to switch to `v4`) was incorrect. v5 is the correct, working API on v0.118;
    the observed 404 was a legitimate "could not find the metric" for a metric name
    not ingested on forge, and the empty `count_errors` output was this parsing bug.
- `query_metric`: a 404 for a nonexistent metric now raises a clean
  `ValueError` with SigNoz's own message (e.g. "could not find the metric X")
  instead of a raw `httpx.HTTPStatusError`. `_client` now surfaces SigNoz's
  structured error text for all non-2xx responses without leaking the API key or
  internal URL.
- `search_traces` / `tail_logs`: rows are now unwrapped from the v5
  `{"data": {...}}` per-row envelope before being returned.

### Removed

- `observability.py::get_tracer()` — dead code (was never called). OTEL tracing
  now lives in `telemetry.py`, wired into every tool call.

### Added

- `list_metrics` is functional again. Instead of returning a hardcoded
  "endpoint removed in v0.118" error dict, it now sources metric names and
  metadata from the `GET /api/v2/metrics` endpoint (the same one the official
  SigNoz MCP server uses), with `search_text`, `start`/`end`, `limit`, and
  `source` parameters. Returns metric metadata dicts (`metricName`, `type`,
  `temporality`, `isMonotonic`, ...).
- `get_field_keys(signal, ...)` — discover filterable field keys for a signal
  (`metrics`/`traces`/`logs`) via `GET /api/v1/fields/keys`.
- `get_field_values(signal, name, ...)` — discover values for a specific field
  key via `GET /api/v1/fields/values`.

## [0.2.0] — 2026-06-14

### Breaking

- `SIGNOZ_QUERY_VERSION` default changed from `v3` to `v5`; `v3` is no longer an
  allowed value. SigNoz removed the `/api/v3/query_range` endpoint in v0.118.
- `list_metrics`: endpoint `/api/v1/metricsNames` was removed in SigNoz v0.118.
  The tool now returns a dict with an `error` key explaining the limitation instead
  of a list of strings. Use the SigNoz UI Metrics Explorer as a replacement.

### Fixed

- All query tools (`count_errors`, `search_traces`, `tail_logs`, `count_log_errors`,
  `query_metric`): migrated from the removed `/api/v3/query_range` to
  `/api/v5/query_range`. All tools were returning `400 panel type is invalid` against
  SigNoz v0.118+ (SGNZ-1, SGNZ-2).
- `query_metric`: v5 API requires `metricName` inside the aggregation object alongside
  `timeAggregation`/`spaceAggregation`; removed the v3-style top-level `metricName`
  field and `expression`-based aggregation format.
- `count_log_errors`: `groupBy` field changed from `serviceName` (not found in v5 logs
  schema) to `resource.service.name` (OTel resource attribute).
- `tail_logs`: log filter changed from `severityText` (v3 field name) to `severity_text`
  (v5 field name); service-name filter removed from the query filter expression because
  `resource.service.name` is not available in the v5 log filter parser without data in
  the schema registry.
- `_client.py`: removed `variables: {}` top-level field from query payload (not accepted
  by v5 endpoint).
- Response parsing updated for the v5 `data.data.results[].aggregations[]` shape
  (v3 used `data.result[].metric` / `data.result[].values`).

## [0.1.3] — 2026-05-30

### Fixed

- `list_services`: corrected endpoint from `/api/v1/services` (returns SPA HTML on v0.118+)
  to `/api/v1/services/list`; updated return type from `list[dict]` to `list[str]`
- `list_alert_rules`: fixed data extraction — `/api/v1/rules` returns
  `{"data":{"rules":[]}}`, not `{"data":[]}`, causing `KeyError` on `[:200]` slice

### Security

- `query_metric`: validate `label_filter` against `[a-zA-Z0-9_.='<>!()\[\]\s,]+` allowlist
  before sending to SigNoz query API (LOW-01 — 2026-05-30/signoz-mcp-deploy-2026-05)
- `observability.py`: log directory created with mode 0750; log file chmod'd to 0640 on
  startup (NE-05/FW-07 — 2026-05-30/signoz-mcp-deploy-2026-05)

## [0.1.2] — 2026-05-27

### Security

- `query_metric`: validate `metric_name` against `[a-zA-Z0-9._:/-]+` allowlist; raise
  `ValueError` on invalid input (L1 — security audit forge-observer-mcps-deploy)
- `query_metric`: cap `label_filter` at 500 chars (L1 — same audit)

## [0.1.1] — 2026-05-27

### Added

- `observability.py` — structured logging always on (stderr, JSON, structlog);
  default log path `/opt/appdata/signoz-mcp/logs/signoz-mcp.log`; log directory
  created at startup; OTEL tracing opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`.
- `configure_logging()` wired into `main()` before `mcp.run()`.
- `[otel]` optional dep group: `opentelemetry-sdk>=1.20`,
  `opentelemetry-exporter-otlp-proto-grpc>=1.20`.
- Bare `LOG_FILE` guard: `if log_dir:` check before `os.makedirs` prevents
  `FileNotFoundError` when `LOG_FILE` is set to a bare filename.

## [0.1.0] — 2026-05-27

### Added

- Initial release: FastMCP Python MCP server for SigNoz observability queries
- 9 read-only tools: `list_services`, `count_errors`, `search_traces`, `tail_logs`,
  `count_log_errors`, `query_metric`, `list_metrics`, `list_alert_rules`, `get_health`
- `SIGNOZ_API_KEY` required at startup, validated, never logged
- `SIGNOZ_QUERY_VERSION` allowlisted to `v3` / `v5` at startup
- Input validation: `service` names allowlisted (alphanumeric/dash/underscore/dot);
  `severity` values allowlisted (TRACE/DEBUG/INFO/WARN/ERROR/FATAL)
- Response size caps: raw/trace queries max 500; aggregate queries max 10000;
  `list_metrics` capped at 500; `list_services`, `list_alert_rules`, `query_metric` capped at 200
- Time parameter format: relative durations (`-1h`, `-30m`, `-7d`) converted to epoch milliseconds
- 23 tests with respx mocks — 91% coverage
- PM2 ecosystem config for forge deployment
