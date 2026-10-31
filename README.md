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

signoz-mcp is not published to PyPI. Install it from a checkout of a release tag, from the
committed `uv.lock`, so the dependency set is the one CI tested and audited. The package is
installed into the venv as a copy (not editable), so the code that runs is the release's,
and its recorded version is true. Use [uv](https://docs.astral.sh/uv/).

Save this as a script and run it with `bash`. Don't paste it into a shell: every check ends
the run on failure, and that only works under `set -e` in a script.

```bash
#!/usr/bin/env bash
set -euo pipefail
VERSION=0.5.0
# The directory the service runs from: ecosystem.config.js `cwd`.
REPO=${REPO:-$HOME/repos/personal/signoz-mcp}
PY=/usr/bin/python3.13
EXTRAS=(--extra telemetry)   # drop it if no telemetry backend will ever be enabled
die() { echo "install: $*" >&2; exit 1; }
tmp=$(mktemp -d) && trap 'rm -rf -- "$tmp"' EXIT

[ -d "$REPO/.git" ] || git clone https://github.com/TadMSTR/signoz-mcp "$REPO"
cd "$REPO" || die "cannot enter $REPO"
# 0. Stop the service and move its venv aside (outside the checkout) BEFORE this runs.
#    Until it is replaced, an editable venv runs this checkout's source, so checking out
#    the release under a running service would change the code it runs.
[ ! -e .venv ] || die ".venv exists: stop the service and move .venv aside first"
git fetch --tags origin
git checkout --quiet "v$VERSION"

# 1. The checkout is exactly the release tag on the remote, and nothing else. The service
#    starts with `python -m signoz_mcp.server` from this directory, and `python -m` puts
#    the directory first on sys.path, so anything importable here would shadow the
#    installed package. Python also loads a module from __pycache__ whenever the cached
#    header's source mtime and size match, and `git status` does not show ignored files,
#    or tracked files flagged assume-unchanged or skip-worktree. Remove bytecode and tool
#    caches, then refuse any flagged file and anything left except the venv. A `.env` is
#    refused too: fastmcp reads it from this directory as configuration.
find . -path ./.venv -prune -o \( -name __pycache__ -o -name '*.py[co]' \) -print0 \
  | xargs -0 -r rm -rf --
rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist ./*.egg-info src/*.egg-info
flagged=$(git ls-files -v | grep -v '^H ' || true)
[ -z "$flagged" ] || die "tracked files flagged out of git status; clear the flags first:
$flagged"
status=$(git status --porcelain --ignored)
extra=$(printf '%s\n' "$status" | grep -vxE '(!! \.venv/)?' || true)
[ -z "$extra" ] || die "checkout holds more than v$VERSION; remove these first:
$extra"
# The peeled line (^{}) is listed last for an annotated tag; a lightweight tag has one line.
tag=$(git ls-remote origin "refs/tags/v$VERSION" "refs/tags/v$VERSION^{}" | tail -n1 | cut -f1)
[ -n "$tag" ] || die "tag v$VERSION is not on origin"
[ "$(git rev-parse HEAD)" = "$tag" ] || die "HEAD is not origin's v$VERSION ($tag)"

# 2. Build a fresh venv at the path the service runs from. Pin the interpreter: uv
#    otherwise prefers a Python it manages itself, which `uv python` can later remove or
#    replace under a running service. --no-cache: build this project from this tree, not
#    from a wheel uv cached on an earlier run.
UV_PROJECT_ENVIRONMENT="$REPO/.venv" \
  uv sync --frozen --no-editable --no-cache "${EXTRAS[@]}" --python "$PY"

# 3. The venv is exactly the lock (no missing, extra or different package), and the
#    installed package is byte-for-byte the release: build a wheel from the tag's own
#    objects (`git archive`) and compare every file with what was installed.
UV_PROJECT_ENVIRONMENT="$REPO/.venv" \
  uv sync --frozen --no-editable "${EXTRAS[@]}" --python "$PY" --check \
  || die "venv does not match uv.lock"
mkdir "$tmp/src"
git archive "$tag" | tar -x -C "$tmp/src"
uv build --wheel --no-cache --python "$PY" --out-dir "$tmp/dist" "$tmp/src" >/dev/null
.venv/bin/python -I - "$tmp"/dist/*.whl .venv/lib/python3.13/site-packages <<'EOF' \
  || die "installed package differs from the v$VERSION wheel"
import sys, zipfile, pathlib
whl, site = zipfile.ZipFile(sys.argv[1]), pathlib.Path(sys.argv[2])
names = [n for n in whl.namelist() if not n.endswith("/") and ".dist-info/" not in n]
bad = [n for n in names if not (site / n).is_file() or (site / n).read_bytes() != whl.read(n)]
tops = {n.split("/")[0] for n in names}
extra = sorted(str(p.relative_to(site)) for t in tops for p in (site / t).rglob("*")
               if p.is_file() and "__pycache__" not in p.parts
               and str(p.relative_to(site)) not in names)
assert not bad and not extra, f"differ: {bad} extra: {extra}"
print(f"install: {len(names)} files match the release wheel")
EOF
# The package imports from the venv, not from the checkout, when started the way PM2
# starts it: from this directory. The server refuses to import without SIGNOZ_API_KEY; a
# placeholder is enough to import.
SIGNOZ_API_KEY=import-check .venv/bin/python - "$VERSION" <<'EOF' \
  || die "installed package check failed"
import importlib.metadata as m, sys, signoz_mcp, signoz_mcp.server
assert "/site-packages/" in signoz_mcp.__file__, signoz_mcp.__file__
assert m.version("signoz-mcp") == sys.argv[1], m.version("signoz-mcp")
print("signoz-mcp", sys.argv[1], "from", signoz_mcp.__file__)
EOF
echo "install: signoz-mcp v$VERSION ready; restart the service by name"
```

Build a fresh venv rather than installing into an existing one: an install into an existing
venv keeps whatever older dependency versions it finds there, if they still satisfy the
declared ranges. Build at the final path rather than building elsewhere and moving it: the
venv's scripts embed its path. A uv-built venv has no `pip`. Stop the service and move
its venv aside before running the script (step 0 refuses to run otherwise), and restart it
after step 3. Set `REPO` if the service runs from somewhere else. Under PM2, restart it by name
(`pm2 restart signoz-mcp`): `ecosystem.config.js` reads `SIGNOZ_API_KEY` from the shell that
first started it, so a `pm2 delete` and `start`, or `--update-env`, from a shell without the
key loses it.

CI audits the locked set on every push and weekly, but nothing reads a scheduled run's
result unless you watch it, and GitHub disables a public repository's scheduled workflows
after 60 days without activity. Audit the deployed venv where it runs as well (for example
`pip-audit --path .venv/lib/python3.13/site-packages`).

## Running

```bash
SIGNOZ_API_KEY=<token> python -m signoz_mcp.server
# or via PM2:
pm2 start ecosystem.config.js
```

## Development

```bash
uv sync --frozen --extra dev --extra telemetry
uv run --frozen --no-sync pytest
uv run --frozen --no-sync pytest --cov=signoz_mcp --cov-report=term-missing
uv run --frozen --no-sync ruff check .
uv run --frozen --no-sync ruff format .
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
