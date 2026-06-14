# Changelog

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
