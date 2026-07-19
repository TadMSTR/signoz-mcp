# Telemetry

Logging (structlog JSON) is **on by default**. Metrics and tracing are **off by default**
and turn on per-backend when the relevant environment variable is set. Install the optional
dependencies with:

```bash
pip install 'signoz-mcp[telemetry]'
```

Every tool call records three signals — **call count**, **error count**, and **upstream
latency (seconds)** — plus an OTLP span. All credentials are read from the environment
only; the InfluxDB/NATS sinks are best-effort and fire-and-forget, so a telemetry backend
being down never breaks a tool call.

> The `SIGNOZ_MCP_` prefix on the telemetry env vars is deliberate — it keeps this
> server's *own* telemetry configuration separate from the `SIGNOZ_URL` /
> `SIGNOZ_API_KEY` / `SIGNOZ_QUERY_VERSION` variables, which configure the **upstream**
> SigNoz connection that the tools query.

## OTLP traces + metrics (SigNoz)

| Env var | Effect |
|---------|--------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | enable OTLP span + metric export (e.g. `http://signoz-otel-collector:4317`) |

Spans are named `tool.<name>`; metrics are `signoz_mcp.tool.calls`,
`signoz_mcp.tool.errors`, `signoz_mcp.tool.latency`.

## InfluxDB 3

forge runs `influxdb:3-core` — this uses the **v3** write API (`influxdb3-python`,
imported as `influxdb_client_3`), not the 2.x client.

| Env var | Default | Notes |
|---------|---------|-------|
| `SIGNOZ_MCP_INFLUXDB3_URL` | *(unset → disabled)* | enables the sink when set |
| `SIGNOZ_MCP_INFLUXDB3_TOKEN` | `""` | auth token |
| `SIGNOZ_MCP_INFLUXDB3_DATABASE` | `signoz_mcp` | target database/bucket |

Writes a `tool_call` measurement tagged by `tool`/`status` with `latency_s` and `count`.

## NATS

| Env var | Default | Notes |
|---------|---------|-------|
| `SIGNOZ_MCP_NATS_URL` | *(unset → disabled)* | enables the sink when set (e.g. `nats://127.0.0.1:4222`) |
| `SIGNOZ_MCP_NATS_SUBJECT` | `signoz_mcp.metrics` | subject to publish JSON metric events on |

Publishes one JSON message per call: `{tool, latency_s, status, error}`.

## Disabled path

With none of the above set, the telemetry layer is a transparent timer — no providers, no
connections, no dependencies required. This is the CI/base-install path.
