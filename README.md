# signoz-mcp

[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

FastMCP Python MCP server for SigNoz observability queries. Gives agents read-only
access to services, traces, logs, metrics, and alert rules via the SigNoz HTTP API.
Targets the SigNoz **v5** query API (v0.118+).

## Tools

| Tool | Description |
|------|-------------|
| `list_services` | Services seen in a time window, with their RED metrics — returns `list[dict]` (`serviceName`, `p99`, `avgDuration`, `numCalls`, `callRate`, `numErrors`, `errorRate`, `num4XX`, `fourXXRate`). Takes `start`/`end` |
| `search_traces` | Search traces by a free-form filter expression + shortcut params (service, operation, error, duration bounds) |
| `aggregate_traces` | Aggregate traces (count/count_distinct/avg/sum/min/max/p50–p99/rate) grouped by field(s); scalar or time_series |
| `get_trace_details` | Every span in a trace (`include_spans=True`) or a one-row trace summary |
| `tail_logs` | Most recent logs at a severity, newest first. No `service` parameter — see [vikunja#926](#logs) |
| `search_logs` | Search logs by a free-form filter expression + shortcut params (service, severity, body search) |
| `aggregate_logs` | Aggregate logs grouped by field(s); scalar or time_series |
| `query_metric` | Named metric time series with an optional label filter |
| `list_metrics` | Search/list ingested metric names + metadata (type, temporality, …) |
| `get_field_keys` | Discover filterable field keys for a signal (metrics/traces/logs) |
| `get_field_values` | Discover values for a specific field key |
| `list_alert_rules` | Alert rules and current firing state |
| `get_health` | Connectivity check |

### Fleet-operator surface

| Tool | Description |
|------|-------------|
| `execute_builder_query` | Raw SigNoz Query Builder v5 passthrough — the escape hatch when a wrapper's shape is wrong for your question |
| `fleet_health` | Per-service `calls`, `errors`, `error_rate`, `p95_nano`/`p95_ms` — **one** query, so every column comes from the same scan |
| `compare_windows` | Per-group delta between two windows — `before`, `after`, `delta`, `pct_change`. Answers "what changed since the deploy" |

`fleet_health` and `list_services` both return per-service RED-style metrics, but they are
not interchangeable: `list_services`' `p99`/`avgDuration` are scoped to each service's
top-level operations only, while `fleet_health`'s `p95`/`error_rate` come from a single
scan over every span, so all of its columns share one consistent scope. Use
`list_services` for top-level-request latency; use `fleet_health` when you need numbers
that are guaranteed to come from the same scan (e.g. before dividing errors by calls).

All tools are read-only — the server never exposes SigNoz write endpoints.

## Upgrading from 0.3.x

Two tool signatures changed in 0.4.0 — both were silently-wrong-answer paths, so treat a
caller that still runs unchanged as one still getting the wrong answer. Full reasoning is
in [CHANGELOG.md](CHANGELOG.md); the summary:

- **`list_services`** now returns `list[dict]` instead of `list[str]`, and takes
  `start`/`end`. Callers that treated the result as a bare list of service names must
  switch to reading the `serviceName` key off each dict.
- **`tail_logs`** no longer accepts `service`. It previously validated the argument and
  then filtered on severity alone, so passing it never did anything — it is now a
  `TypeError` rather than a silent no-op. Use `search_logs(filter=...)` instead, and read
  the [Logs](#logs) section below first.

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

## Logs

**Nothing on forge exports OTLP logs**, and the collector has no `filelog` receiver, so
SigNoz's log store is empty — measured 0 rows at `-720h`, while `get_health()` and
`aggregate_traces` both pass as controls (vikunja#926). Because of that, **the log tools
currently raise rather than return `[]`.**

Rather than return an empty list that an agent would read as "no matching logs",
`tail_logs` and `search_logs` detect the no-data-at-all case and raise a message naming
the ticket. The distinguisher is that the logs signal reports **only SigNoz's built-in
schema keys** (`fieldContext` `log`/`scope`) and none derived from ingested data
(`resource`/`attribute`). Note that the field-keys payload is *not* empty on an empty
store — it holds eight built-ins — so a check written against emptiness would never fire.

`tail_logs` takes no `service` argument. It previously accepted one, validated it, and
then filtered on severity alone (vikunja#927). Which key names a service in the logs
signal is still unresolved — `search_logs` emits `service.name`, `aggregate_logs`'
docstring recommends `resource.service.name` — and it cannot be settled until there is
log data to test against. Trace-side tools are unaffected.

## Per-tool latency, without a new tool

Agents on forge emit `tool.<name>` spans, and per-tool latency is usually the actionable
unit rather than per-service. `group_by` already reaches it — no separate tool:

```python
# Which tool calls are slowest, across the fleet?
aggregate_traces(
    aggregation="p95", aggregate_on="duration_nano", group_by="service.name,name", start="-168h"
)

# Did a specific tool get slower since yesterday?
compare_windows(
    window_a="-48h",
    window_b="-24h",
    aggregation="p95",
    aggregate_on="duration_nano",
    group_by="name",
    filter="service.name = 'scoped-mcp-developer'",
)
```

`name` is the span name; `service.name,name` groups by both.
