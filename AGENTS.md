---
owner: TadMSTR
github-account: personal
last-updated: 2026-05-27
---

# signoz-mcp — Agent Instructions

## Purpose

FastMCP Python MCP server wrapping the SigNoz HTTP API with 9 read-only tools.
Gives agents direct access to SigNoz observability data — services, traces, logs,
metrics, and alert rules — without a Grafana session or inline HTTP calls.
Deployed on forge as a PM2 process wired into the sysadmin agent's scoped-mcp config.

## Structure

```
signoz_mcp/
  __init__.py     Package marker
  __main__.py     python -m signoz_mcp entry point
  _client.py      Shared httpx client, query helper, auth header construction
  server.py       FastMCP server — all 9 tools, time helpers
tests/
  __init__.py
  test_server.py  pytest + respx tests for all tools and helpers
ecosystem.config.js   PM2 stdio process config
pyproject.toml        Package metadata, deps, ruff + pytest config
```

## Invariants

- All MCP tools are read-only — no POST/PUT/DELETE to SigNoz write endpoints.
- `SIGNOZ_API_KEY` value must never appear in any log output, error message, or exception traceback.
- `SIGNOZ_API_KEY` is sourced from environment only — no config file or `.env` fallback.
- `SIGNOZ_QUERY_VERSION` must be validated against allowlist `["v3", "v5"]` at startup.
- All `service` parameters are validated (alphanumeric, dash, underscore, dot) before use in filter expressions.
- Response sizes are capped before returning to the MCP caller.
- No shell exec, subprocess calls, or filesystem writes.

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastmcp` | MCP server framework |
| `httpx` | Async HTTP client for SigNoz API calls |
| `structlog` | Structured JSON logging |

## Configuration

| Var | Default | Purpose |
|-----|---------|---------|
| `SIGNOZ_URL` | `http://localhost:8080` | SigNoz base URL |
| `SIGNOZ_API_KEY` | required | Service Account token → `SIGNOZ-API-KEY` HTTP header |
| `SIGNOZ_QUERY_VERSION` | `v3` | API path version; `v3` or `v5` only |

Raises `RuntimeError` at startup if `SIGNOZ_API_KEY` is empty or unset.

## Extension points

- **Add new tools:** `signoz_mcp/server.py` — follow existing `@mcp.tool()` pattern; add corresponding tests
- **Do not modify:** `signoz_mcp/_client.py` auth header construction without security review

## Out of scope for agents

- Implementing any write operations against SigNoz
- Adding `.env` file loading or config file fallbacks for secrets
- Changing `SIGNOZ_API_KEY` handling without explicit approval

## Security notes

- Auth header name is `SIGNOZ-API-KEY` (hyphen, not underscore) per SigNoz Service Account API docs
- SigNoz API version is allowlisted: only `v3` and `v5` accepted for `SIGNOZ_QUERY_VERSION`
- Input validation on `service` param prevents filter expression injection
- Limits: raw/trace queries capped at 500; aggregate queries capped at 10000; time ranges max 24h (raw/trace) or 7d (aggregates)

## Testing

```bash
pip install -e ".[dev]"
pytest
pytest --cov=signoz_mcp --cov-report=term-missing
```

Tests use `respx` to mock the SigNoz HTTP API. No real network calls. Coverage threshold: 80%.

## Git workflow

Branch before editing — do not commit directly to `main`.
