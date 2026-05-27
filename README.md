# signoz-mcp

FastMCP Python MCP server for SigNoz observability queries. Gives agents read-only
access to services, traces, logs, metrics, and alert rules via the SigNoz HTTP API.

## Tools

| Tool | Description |
|------|-------------|
| `list_services` | All services with RED metrics |
| `count_errors` | Error span count grouped by service over a time range |
| `search_traces` | Filter traces by service, error state, minimum duration |
| `tail_logs` | Recent logs for a service filtered by severity |
| `count_log_errors` | Error/warn log rate over time |
| `query_metric` | Named metric with optional label filter |
| `list_metrics` | All ingested metric names |
| `list_alert_rules` | Alert rules and current firing state |
| `get_health` | Connectivity check |

## Configuration

| Variable | Default | Required |
|----------|---------|----------|
| `SIGNOZ_URL` | `http://localhost:8080` | No |
| `SIGNOZ_API_KEY` | — | **Yes** |
| `SIGNOZ_QUERY_VERSION` | `v3` | No |

`SIGNOZ_API_KEY` must be a SigNoz Service Account token. Create one at
**Settings → Integrations → Service Accounts** in the SigNoz UI.

`SIGNOZ_QUERY_VERSION` accepts `v3` or `v5`. Defaults to `v3` (confirmed working
on forge running SigNoz v0.118.0).

## Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
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
