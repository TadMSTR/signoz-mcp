# Extension hooks

signoz-mcp exposes a **pre/post hook** system so third parties can intercept tool calls
without editing the server — the same pattern as the other forge MCP servers. Because this
is a single-server process, hooks key on the **tool name alone** rather than
`(server, tool)`.

## Where hooks fire

Every tool is registered through `server.tool`, which wraps it in `server.instrument`:

```
call → run_before_hooks(tool, kwargs) → [telemetry span/metric] → tool(**kwargs)
     → run_after_hooks(tool, result) → return
```

Because the wrapper is applied uniformly (and preserves each tool's signature via
`__signature__`, so FastMCP's schema is unchanged), a hook registered for a tool is
guaranteed to fire around every invocation of it — including calls that arrive over MCP.

## API

| Function | Purpose |
|----------|---------|
| `register_before(tool, handler)` | `async def handler(kwargs: dict) -> dict` — inspect/mutate args |
| `register_after(tool, handler)`  | `async def handler(result) -> result` — inspect/transform result |
| `run_before_hooks(tool, kwargs)` | fire the before-chain (called by `instrument`) |
| `run_after_hooks(tool, result)`  | fire the after-chain (called by `instrument`) |
| `clear_hooks()` | drop all registrations (tests only) |

## Contract

- Handlers run in **registration order**; each receives the previous handler's output.
- Handlers are **not** fire-and-forget. An exception propagates to the caller; a `before`
  exception aborts the chain and prevents the tool (and the upstream SigNoz call) running.
- `tool` is the tool's Python function name (`"search_traces"`, `"query_metric"`, …).

## Example

```python
from signoz_mcp.hooks import register_before

async def force_recent_window(kwargs: dict) -> dict:
    # Cap unbounded log tails to the last 15 minutes.
    kwargs.setdefault("start", "-15m")
    return kwargs

register_before("tail_logs", force_recent_window)
```

## Bundled audit-log hook

`signoz_mcp/contrib/audit_log.py` ships a ready-made **before** hook that logs one
structured line per call — the tool name, the caller (`anonymous`, since signoz-mcp uses a
single shared service-account key rather than per-caller tokens), and a **hash** of the
arguments (never the raw values). `server.main` registers it across all tools at startup;
register it for a subset yourself with:

```python
from signoz_mcp.contrib.audit_log import register_audit_log

register_audit_log(["search_traces", "search_logs", "tail_logs"])
```
