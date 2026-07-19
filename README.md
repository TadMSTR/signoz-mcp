# signoz-mcp

FastMCP Python MCP server for SigNoz observability queries. Gives agents read-only
access to services, traces, logs, metrics, and alert rules via the SigNoz HTTP API.
Targets the SigNoz **v5** query API (v0.118+).

## Tools

| Tool | Description |
|------|-------------|
| `list_services` | All registered service names — returns `list[str]` |
| `search_traces` | Search traces by a free-form filter expression + shortcut params (service, operation, error, duration bounds) |
| `aggregate_traces` | Aggregate traces (count/count_distinct/avg/sum/min/max/p50–p99/rate) grouped by field(s); scalar or time_series |
| `get_trace_details` | Every span in a trace (`include_spans=True`) or a one-row trace summary |
| `tail_logs` | Recent logs filtered by severity |
| `search_logs` | Search logs by a free-form filter expression + shortcut params (service, severity, body search) |
| `aggregate_logs` | Aggregate logs grouped by field(s); scalar or time_series |
| `query_metric` | Named metric time series with an optional label filter |
| `list_metrics` | Search/list ingested metric names + metadata (type, temporality, …) |
| `get_field_keys` | Discover filterable field keys for a signal (metrics/traces/logs) |
| `get_field_values` | Discover values for a specific field key |
| `list_alert_rules` | Alert rules and current firing state |
| `get_health` | Connectivity check |

All tools are read-only — the server never exposes SigNoz write endpoints.

## Configuration

| Variable | Default | Required |
|----------|---------|----------|
| `SIGNOZ_URL` | `http://localhost:8080` | No |
| `SIGNOZ_API_KEY` | — | **Yes** |
| `SIGNOZ_QUERY_VERSION` | `v5` | No |

`SIGNOZ_API_KEY` must be a SigNoz Service Account token. Create one at
**Settings → Integrations → Service Accounts** in the SigNoz UI.

`SIGNOZ_QUERY_VERSION` only accepts `v5` — SigNoz removed the v3/v4 query_range
formats in v0.118. Any other value fails fast at startup.

### Telemetry (optional, off by default)

Structured logging (structlog JSON) is always on. OTLP spans+metrics and the
InfluxDB 3 / NATS metric sinks are opt-in per backend via environment variables and
require the `telemetry` extra. See [`docs/telemetry.md`](docs/telemetry.md). Third
parties can intercept tool calls without editing the server — see
[`docs/extension-hooks.md`](docs/extension-hooks.md).

## Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
# with telemetry backends:
pip install -e ".[telemetry]"
```

## Running

```bash
SIGNOZ_API_KEY=<token> python -m signoz_mcp.server
# or via PM2:
pm2 start ecosystem.config.js
```

## Development

```bash
pip install -e ".[dev]"
pytest
pytest --cov=signoz_mcp --cov-report=term-missing
ruff check .
ruff format .
```
