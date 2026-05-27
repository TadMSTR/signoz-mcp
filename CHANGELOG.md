# Changelog

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
