---
owner: TadMSTR
github-account: personal
last-updated: 2026-07-19
---

# signoz-mcp — Agent Instructions

## Purpose

FastMCP Python MCP server wrapping the SigNoz HTTP API with 13 read-only tools.
Gives agents direct access to SigNoz observability data — services, traces, logs,
metrics, and alert rules — without a Grafana session or inline HTTP calls.
Targets the SigNoz v5 query API (v0.118+). Deployed on forge as a PM2 process wired
into the sysadmin agent's scoped-mcp config.

## Structure

```
signoz_mcp/
  __init__.py     Package marker
  __main__.py     python -m signoz_mcp entry point
  _client.py      Shared httpx client, query helper, auth header, error sanitizing
  server.py       FastMCP server — all tools, time helpers, v5 response parsing,
                  and the instrument/tool wrapper (hooks + telemetry)
  telemetry.py    Optional OTLP/InfluxDB3/NATS telemetry (off by default)
  hooks.py        Pre/post extension-hook registry
  contrib/
    audit_log.py  Read-only audit-log before-hook (who/what/args-hash)
tests/
  test_server.py       tool + helper tests (respx-mocked)
  test_hooks.py        hook registry + instrument wiring
  test_telemetry.py    telemetry disabled-path + _emit fan-out
  test_contrib_audit.py audit-log hook
docs/
  telemetry.md, extension-hooks.md
ecosystem.config.js   PM2 process config
pyproject.toml        Package metadata, deps, ruff + pytest config
```

## Invariants

- All MCP tools are read-only — no POST/PUT/DELETE to SigNoz write endpoints.
- `SIGNOZ_API_KEY` value must never appear in any log output, error message, or exception traceback.
- `SIGNOZ_API_KEY` is sourced from environment only — no config file or `.env` fallback.
- `SIGNOZ_QUERY_VERSION` must be validated against allowlist `["v5"]` at startup.
- `service` parameters and free-form `filter` expressions are validated against
  allowlists before use (see `_validate_service` / `_validate_filter_expr`).
- Response sizes are capped before returning to the MCP caller.
- No shell exec, subprocess calls, or filesystem writes.
- The telemetry layer must import and run with **zero** optional deps installed
  (disabled path) — every backend import is lazy/guarded.

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastmcp` | MCP server framework |
| `httpx` | Async HTTP client for SigNoz API calls |
| `structlog` | Structured JSON logging |

Optional `[telemetry]` extra: `opentelemetry-sdk`,
`opentelemetry-exporter-otlp-proto-grpc`, `influxdb3-python`, `nats-py`.

## Configuration

| Var | Default | Purpose |
|-----|---------|---------|
| `SIGNOZ_URL` | `http://localhost:8080` | SigNoz base URL |
| `SIGNOZ_API_KEY` | required | Service Account token → `SIGNOZ-API-KEY` HTTP header |
| `SIGNOZ_QUERY_VERSION` | `v5` | query_range API version; only `v5` accepted (v3/v4 removed in v0.118) |

Telemetry env vars (all optional, off by default) — see `docs/telemetry.md`:
`OTEL_EXPORTER_OTLP_ENDPOINT`, `SIGNOZ_MCP_INFLUXDB3_URL`/`_TOKEN`/`_DATABASE`,
`SIGNOZ_MCP_NATS_URL`/`_SUBJECT`.

Raises `RuntimeError` at startup if `SIGNOZ_API_KEY` is empty or unset, or if
`SIGNOZ_QUERY_VERSION` is anything other than `v5`.

## Extension points

- **Add new tools:** `signoz_mcp/server.py` — follow the existing `@tool` pattern
  (instruments the tool with hooks + telemetry); add corresponding tests.
- **Intercept calls:** register pre/post hooks via `signoz_mcp/hooks.py` — see
  `docs/extension-hooks.md`.
- **Do not modify:** `signoz_mcp/_client.py` auth header / error handling without security review.

## Out of scope for agents

- Implementing any write operations against SigNoz
- Adding `.env` file loading or config file fallbacks for secrets
- Changing `SIGNOZ_API_KEY` handling without explicit approval

## Security notes

- Auth header name is `SIGNOZ-API-KEY` (hyphen, not underscore) per SigNoz Service Account API docs.
- SigNoz API version is allowlisted: only `v5` accepted for `SIGNOZ_QUERY_VERSION`.
- Non-2xx responses surface SigNoz's own error message (sanitized) — never the API key or internal URL.
- Input validation on `service` and free-form `filter` expressions bounds the query-injection surface
  (`_FILTER_EXPR_RE` allows identifier/operator/literal characters, excludes `;` `` ` `` `\` and control chars).
- Limits: raw/trace queries capped at 500; aggregate queries capped at 10000.

## Testing

```bash
pip install -e ".[dev]"
pytest
pytest --cov=signoz_mcp --cov-report=term-missing
```

Tests use `respx` to mock the SigNoz HTTP API. No real network calls. Coverage threshold: 80%.

## Git workflow

Branch before editing — do not commit directly to `main`.
