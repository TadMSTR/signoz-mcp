# Changelog

## [0.4.0] — 2026-09-20

### Breaking

Two tool signatures changed. Both were silently-wrong-answer paths, so the break is the
fix rather than a side effect of it — a caller that keeps working unchanged is a caller
still getting the wrong answer.

- **`list_services` returns `list[dict]`, not `list[str]`**, and takes `start`/`end`.
  Each dict carries SigNoz's own field names (`serviceName`, `p99`, `avgDuration`,
  `numCalls`, `callRate`, `numErrors`, `errorRate`, `num4XX`, `fourXXRate`). Callers
  treating the result as a list of strings must read `serviceName`.
- **`tail_logs` no longer accepts `service`.** It validated the argument and then filtered
  on severity alone, so passing it never did anything; it is now a `TypeError` rather than
  a silent no-op. Use `search_logs(filter=...)` — but read the Logs section of the README
  first (vikunja#926).

Version is `0.4.0` rather than `0.3.1` because for a `0.x` package the **minor** is the
breaking boundary — the same rule this build applied to `httpx>=0.27,<0.29`.

### Added — the fleet-operator surface

Three tools an operator running ~25 instrumented services wants and the upstream SigNoz
MCP server does not provide. All trace-side, so none is blocked on #926.

- **`execute_builder_query(signal, request_type, spec, start, end)`** — a thin
  passthrough to Query Builder v5, returning SigNoz's body unparsed. Every other tool
  here is a wrapper, and #322 is what a wrong wrapper costs: callers had no way through.
  Verified live to reproduce `aggregate_traces`' 7d result from a hand-built spec — set
  equality, not a plausible-looking answer.

  It is an escape hatch from this server's **tool shapes, not its input validation**: a
  caller-supplied `filter` expression still goes through `_validate_filter_expr`, and
  `name`/`signal`/`disabled` cannot be overridden from `spec` to reach past the envelope.

  Pre-audit hardening: **every** caller-supplied free-form string in `spec` is validated,
  not just `filter`. `aggregations[].expression` is the same kind of DSL string and
  `groupBy[].name` is a field name; validating one and not the others would leave exactly
  the asymmetry that turns an escape hatch into a bypass. `order[].key.name` takes the
  *filter-expression* allowlist rather than the stricter field-name one, because
  `_build_order` deliberately puts the aggregation expression there — a test asserts
  `count()` is still accepted, which is what caught the over-tight first attempt.

  The validated set was then **measured rather than inferred**. Enumerating only the
  fields this repo's wrapper tools emit gives a narrower set than the v5 API accepts —
  probing live confirmed `having.expression`, `secondaryAggregations[].expression`,
  `secondaryAggregations[].groupBy[]` and `selectFields[].name` all return 200, so all
  four genuinely reached the backend unvalidated. (`filter` as a bare string returns 400,
  and `functions[]` carries no free-form expression.) SigNoz does check `having`
  server-side, but relying on that would make this server's guard depend on a backend
  version.
  Both are asserted in tests.

- **`fleet_health(start, end)`** — per-service `calls`, `errors`, `error_rate`,
  `p95_nano` and `p95_ms`, busiest first. Composing this from `aggregate_traces` takes
  three or four calls; this is **one**, because SigNoz v5 accepts several aggregations on
  a single query (`count()`, `p95(duration_nano)`, `countIf(has_error = true)`).

  One call also means every column comes from the same scan over the same spans. That is
  deliberate: `list_services` returns RED metrics too, but its `p99`/`avgDuration` cover
  top-level operations only, and merging the two sources would put two different scopes
  in adjacent columns of one row. Cross-checked live against an independent
  `aggregate_traces` — call counts match for all 25 services.

- **`compare_windows(window_a, window_b, ...)`** — per-group `before`/`after`/`delta`/
  `pct_change`. A raw count is a stock, not a flow; the operator question is almost
  always the delta.

  Groups present in only one window are **kept**, with 0 on the missing side — a service
  that stopped reporting is exactly what this is for, and dropping it for a tidy join
  would hide the finding. `pct_change` is `None` when `before` is 0, because a new group
  has no percentage change and reporting 0 or infinity would be a fabricated number.

- **Per-span-name grouping needed no new code**, which was checked before writing any:
  `group_by="name"` and `group_by="service.name,name"` already work. Documented in the
  README with two worked examples, both run against live SigNoz before being committed.

### CodeRabbit re-review findings (PR #7, head `9721111`)

The first review was bound to `9a384d1`; the remediation commits changed the CI gates and
`compare_windows`' join logic, so a review at that SHA no longer described the code being
merged. A re-review found two further issues — **both in the remediation itself**.

- **Major: the truncation guard was dead at the maximum limit.**
  `fetch_limit = min(limit + 1, _MAX_LIMIT_AGG)` collapses to `limit` when the caller asks
  for the ceiling, so at most `limit` rows can return and `len(out) > limit` is *always
  false*. The guard added one commit earlier was unreachable at exactly the limit where a
  window is most likely to be truncated — reinstating the fabricated disappearance it
  existed to prevent. The caller-visible cap is now `_MAX_LIMIT_AGG - 1`, reserving the
  over-fetch slot at every reachable limit.
- **Minor: the `limit`/`offset` clamp coerced instead of rejecting.** `int()` accepts
  `1.5`, `"250"` and `True`, so the guard silently *changed* values its own error message
  promised it required to be integers. Now a strict type check, with `bool` excluded
  explicitly because `isinstance(True, int)` is `True` in Python.

**The first test written for the Major fix was vacuous** and is worth recording. It
asserted the `limit` sent in the outgoing spec — which is `_MAX_LIMIT_AGG` under both the
broken and the fixed code, so it passed either way. The observable difference is not what
is *requested* but whether a missing group on a truncated side returns `None` or `0`. Both
fixes are now confirmed red against the pre-fix code, the ceiling one failing on
`assert 0 is None` — the fabricated zero itself.

### CodeRabbit round 3 (full review @ `34d6987`)

One finding, in a test rather than shipped code — the trend across three rounds was
**5 → 2 → 1**, and only this round's finding was not in production code.

- **The live `compare_windows` test did not handle the `None` semantics this build
  introduced.** `r["delta"] == r["after"] - r["before"]` raises `TypeError` on an unknown
  side. It does not fire on forge today (~25 groups against a default `limit` of 1000),
  which is precisely why it needed pinning rather than leaving to chance — it would have
  failed the live suite for a reason unrelated to the contract under test. A counter was
  added alongside the fix so the loop cannot silently assert nothing if every row happens
  to have an unknown side.

Note for future rounds: CodeRabbit is **incremental** and refuses a plain `@coderabbitai
review` of a commit it considers already seen, replying *"Already reviewed the last
commit."* That refusal is not a clean result. `@coderabbitai full review` is the documented
override and is what produced this round.

### Security audit findings (signoz-mcp-standard-defects-2026-09)

Clean audit — no Critical, High or Medium. Two Low, both remediated.

- **F-01: `execute_builder_query` forwarded `limit`/`offset` unbounded.** Every other tool
  here clamps `limit` to `_MAX_LIMIT_RAW`/`_MAX_LIMIT_AGG` before building its spec; the
  passthrough copied the caller's dict straight through. Same asymmetry the expression and
  field-name validation exists to close, in a position that is **not a string** — which is
  exactly why the pass that found those missed it. Read-only backend, so the concern is
  scan cost and response size rather than data access, but a ceiling that applies
  everywhere except the escape hatch is not a ceiling. Now clamped to `1..10_000` and
  `offset >= 0`, with a non-numeric value rejected outright.
- **F-02: gitleaks installed without checksum verification** — already fixed in `1ca2ed8`
  before the audit landed. The audit ran against `9a384d1`, which predates the fix.
  **CodeRabbit and the security agent found this independently**, which is the useful
  signal: two reviewers with different methods converging on the same gap.

Also closed: the `accepted-risks.md` row for signoz-mcp's *"pip-audit absent from venv"*
(Info, accepted 2026-05-31) — resolved by this build's lockfile-reading split audit gates.

**Left open:** whether the measured `execute_builder_query` field set is complete against
SigNoz v0.118's `builder_query` struct. The audit could not independently re-verify it —
the branch is unmerged, so there is no running instance to probe — and confirmed only that
the validation is internally consistent with what was measured. A live probe pass after
deployment, or a read of SigNoz's struct definition, would close it properly.

### CodeRabbit review findings (PR #7)

Five findings, all valid, all fixed. Two are notable for being the same class of defect
this build had already fixed once — unpinned tooling — in places I had not looked.

- **The gitleaks gate prover accepted any non-zero exit as "detected".** gitleaks uses `1`
  for an operational error *and*, by default, `1` for a finding — so a scan that broke
  before reading anything would have been reported as a working gate. A false-green inside
  the probe written to prevent false-greens. Both the probe and the workflow now pass
  `--exit-code 42`, and treat `1` as a scanner error. Measured against the pinned 8.28.0:
  planted → 42, clean → 0, missing source → 1, malformed config → 1. Verified by running
  the probe against a stub gitleaks that always exits 1 — it now fails, where before it
  would have said "ok detected".
- **`pip-audit` was unpinned.** `uv tool run pip-audit` resolves fresh on every run and
  `uv.lock` does not constrain that environment, so the tool deciding whether this repo
  ships a vulnerable dependency was itself floating to latest. Now
  `--from "pip-audit==2.10.1"`. Worse in consequence than the `ruff` case below: a
  formatter that changes its mind turns CI red, an audit tool that changes its mind can
  turn it **green**.
- **The gitleaks archive was downloaded without an integrity check.** Version pinned,
  artefact not — and a release asset can be replaced under an existing tag. Now verified
  against the SHA-256 published in `gitleaks_8.28.0_checksums.txt`, confirmed against an
  independently downloaded copy, and checked *before* extraction so it fails closed.
- **`AGENTS.md` still claimed a coverage threshold of 80%** after the floor was ratcheted
  to 87 — the documentation encoding the old value, which is how a contributor validates
  against the wrong requirement. Corrected, and pointed at `pyproject.toml` as the single
  source of truth.
- **`compare_windows` could fabricate a disappearance.** Each window is queried
  independently with the same `limit`, ordered by the aggregation descending. A group
  ranking below the cut in one window was absent from that window's response, and the join
  filled the gap with `0` — reporting a service that merely ranked low as having
  **vanished, with a -100% change**. A fabricated finding in the one tool whose entire job
  is saying what changed.

  Fixed by over-fetching `limit + 1`, which makes truncation a fact rather than a
  suspicion — "returned exactly `limit`" is ambiguous, since a window with exactly `limit`
  groups is complete and indistinguishable from a truncated one. When a window was
  truncated, a missing group's side is reported as **`None` (unknown)** rather than `0`,
  and `delta`/`pct_change` are `None` too rather than arithmetic against a value nobody
  measured. Unknown deltas sort last. A control test pins that an **un**truncated window
  still reports a real disappearance as `0` with a real delta — otherwise the guard would
  have destroyed the tool's main use case.

### CI fixes found by the new gates themselves

Both were caught on the PR's first CI run, by gates this build added. Recorded because
in both cases the local result was green and the CI result was the true one.

- **The gitleaks gate was inert under the pinned version.** The planted value was 36
  repeated `A`s; gitleaks **8.28.0** applies an entropy floor to its `github-pat` rule, so
  that string scores 0.67 and is not reported, while the forge host's unpinned
  `/usr/bin/gitleaks` has no such floor and did report it. The probe passed locally
  against a looser binary while the gate could not fail in CI. Planted value now scores
  5.22 and is detected by both; the probe prints the gitleaks version so a skew is
  visible.

- **`ruff` was unpinned, so the formatter gate depended on when you ran it.** Local
  0.15.22 vs CI 0.16.8: `ruff format --check` went red in CI on files `ruff format` had
  just cleaned locally, because 0.16 formats Python code blocks **inside Markdown** and
  0.15 does not. Now pinned `ruff==0.16.8`, with bumps arriving through Dependabot's
  batched dev PR where a formatting change is visible in one diff.

### Fixed

- **`list_services` returned an incomplete set (vikunja#322).** It called
  `GET /api/v1/services/list`, which takes **no time range** and applies its own short
  implicit window. Measured live against SigNoz v0.118.0 on 2026-09-20: that endpoint
  returned **16** services regardless of the window requested, while
  `POST /api/v1/services` with an explicit window returned **21 at 24h and 25 at 7d** —
  set-identical to `aggregate_traces(count, group_by="service.name")` at both. Nine
  services were missing over seven days, including `scoped-mcp-doc-health`,
  `memsearch-summarize` and `nats-mcp`.

  This was a silently-wrong answer, not an error: every caller got a plausible list.

### Changed

- **`tail_logs` validated a `service` argument and then discarded it (vikunja#927).**
  `service` was its only *required* parameter; it was validated at the top of the body
  and never referenced again, because the spec filtered on `severity_text` alone. The
  docstring recorded this as a design note, which is what kept it from looking like a
  bug. It returned nothing today only because the store is empty — the moment #926 is
  fixed it becomes a silent wrong-answer path.

  **The parameter is dropped, not wired up.** Scoping it means choosing a filter key,
  and the two log tools disagree about which key that is (`search_logs` emits
  `service.name`; `aggregate_logs`' docstring recommends `resource.service.name`, while
  its own body emits `service.name`). With no log data, any choice is untestable — and
  this tool is already the result of one guess written up as a decision. The
  disagreement is now recorded in a code comment against #926 rather than silently
  resolved. **Breaking for callers passing `service=`.**

- **Empty log results stop being ambiguous.** Every log tool returned `[]` for both "no
  matching logs" and "this backend holds no logs at all", and an agent cannot tell those
  apart — which is what produced vikunja#909, parse errors diagnosed against an empty
  table. `tail_logs` and `search_logs` now check, **on the empty path only**, whether the
  logs signal carries any field key derived from ingested data, and raise naming #926 if
  not.

  The mechanism is *not* the obvious one. This build's plan proposed treating an empty
  `get_field_keys(signal="logs")` as the signal; measured against SigNoz v0.118.0 on
  2026-09-20 that payload is **not empty** on an empty store — it carries eight built-in
  schema keys at `fieldContext` `log`/`scope`. A guard written that way could never have
  fired. What actually distinguishes the two is `resource`/`attribute` context keys,
  which only exist once something has been ingested: **logs 0, traces 156, metrics 63**.
  A test asserts the guard still fires against a *non-empty* key payload, so the weaker
  version cannot be reintroduced silently.


- **`list_services` is now time-bounded and returns dicts.** New `start`/`end` parameters
  (defaults `-1h`/`now`), for parity with `search_traces`, `aggregate_traces` and
  `list_metrics`. The return type changes from `list[str]` to `list[dict]` carrying
  SigNoz's own field names — `serviceName`, `p99`, `avgDuration`, `numCalls`, `callRate`,
  `numErrors`, `errorRate`, `num4XX`, `fourXXRate`. **This is a breaking change for
  callers that treated the result as a list of strings.**

  `p99`/`avgDuration` are nanoseconds, verified against `p99(duration_nano)` from the
  trace aggregate. They are computed over each service's TOP-LEVEL operations, so they
  do not match the trace aggregate exactly — within ~7% for most services on forge,
  diverging up to 2x where span trees are deep. The docstring says so; use
  `aggregate_traces` when you need all spans. `dataWarning` is dropped (its only member
  is `topLevelOps`, which includes SigNoz's synthetic `overflow_operation` entry).

- **`_client.post(path, json_body)`** — `query()` is hardcoded to the query_range URL
  and `get()` cannot carry a body, so the fix needed a third entry point. It reuses
  `_check_response`, so the sanitized-error and never-leak-the-key contract holds
  identically; that is asserted in tests rather than assumed.

  `start`/`end` on this endpoint must be **JSON strings of nanoseconds**. Numbers return
  `400 json: cannot unmarshal number into Go struct field GetServicesParams.start of
  type string`.

### Testing

- **`tests/test_live_signoz.py`** — integration tests against a real SigNoz. Three of
  this repo's shipped defects were invisible to its 845-line suite *because that suite
  mocks the API*, and no mocked test can assert a result is COMPLETE. The #322
  regression test asserts **set equality against `aggregate_traces`**, not a row count:
  a count assertion passes on the broken version whenever the counts coincide.

  These skip unless `SIGNOZ_LIVE=1`. **A skip is not a pass** — the marker is registered
  in `pyproject.toml` so the state is named rather than silent.

- The pre-existing `test_list_services_returns_list` mocked the *broken* endpoint and
  passed against the defect. **Retargeted onto the replacement rather than deleted** —
  the coverage was real, it was pointed at the wrong endpoint. All four replacement
  tests were confirmed RED against the shipped implementation in an isolated venv before
  the fix landed (an editable install makes a worktree copy insufficient to prove this).

### Repo standard — Baseline under corrected attributes

`repo-index.md` declared `publishes: none, deployed: true` and said nothing about
visibility, while the repo has been **public on GitHub** the whole time. Declaring
`visibility: public` honestly (agent-platform-templates@dbcd0be) pulled in five
requirements that had been scoring N/A. Conformance went **11 pass / 6 fail / 1 skip →
23 pass / 0 fail / 0 skip**; the jump in failures on the declaration alone was the
finding, not a regression.

- **`src/` layout** (P2) — `signoz_mcp/` → `src/signoz_mcp/`, with `where = ["src"]`
  in `[tool.setuptools.packages.find]`. No behavioural change.
- **README badge row** (B2) — Claude Code first, License last.
- **`.gitignore` covers key material** (B6) — `*.key`, `*.pem`, `*.p12`, `*.pfx`.
- **Secret scanning as a CI gate** (B14) — `.github/workflows/secret-scan.yml`, gitleaks
  pinned to 8.28.0 and installed in-job rather than assumed present. Gates push/PR and
  runs a weekly **full-history** scan at `fetch-depth: 0`.
- **`.github/CODEOWNERS`** (F2), **CodeQL** (F3, `security-and-quality`, with the
  `actions` language alongside `python`), **OSSF Scorecard** (F4, `publish_results: true`,
  weekly + `branch_protection_rule`).
- **Top-level `permissions:` in every workflow** (F6) — `release.yml` previously held
  `contents: write` at the top level, granting write to both its jobs; it now reads at
  the top and escalates only in `create-github-release`.
- **Committed `uv.lock`** (F5) **with `.github/dependabot.yml` behind it** — ecosystem
  `uv`, not `pip` (`pip` would update `pyproject.toml` and leave the lock frozen). A
  lockfile with no updater is a freeze, not an improvement (vikunja#670).
- **Bounded version ranges** — `httpx>=0.27,<0.29` and `structlog>=24.0,<26.0` replace
  bare floors. Not cosmetic: structlog's latest is 26.1.0, so the old `>=24.0` floor
  resolved to an untested major. The lock now records 25.5.0 (vikunja#627).
- **The dependency audit reads the lockfile instead of re-resolving.** `pip-audit
  --strict .` against a project with no lockfile could only report on a fresh resolve,
  not on the set this project runs (vikunja#633). Now `uv lock --check` for currency,
  then **two separate gates** — runtime and dev — each run from its own directory,
  because PEP 751 allows `pylock.<name>.toml` and a shared directory makes `pip-audit
  --locked` silently **merge** them (measured here: 89 runtime + 84 dev → 99 merged),
  losing the runtime-vs-dev distinction the split exists to draw.
- **Coverage floor ratcheted 80 → 87** (F8) with its measured number and date in a
  comment beside it. The old floor sat six points below real coverage, so the gate
  would not have gone red on a six-point regression (vikunja#680). Worth recording the
  intermediate step: after the #322 fix landed, coverage fell to 85.88% and the gate
  went **red**. The response was to test the new code, not to lower the floor.

Each new gate was verified **two-sided** before landing — proven to fail on a planted
violation as well as pass on the clean tree:

| Gate | Fails on | Passes on |
|---|---|---|
| gitleaks (B14) | synthetic **high-entropy** PAT committed to a throwaway copy → exit 1 | real tree → exit 0 |
| `uv lock --check` | a dependency added to `pyproject.toml` → exit 1 | in-sync lock → exit 0 |
| `pip-audit --locked` | jinja2 2.11.3 planted in a lock copy → exit 1, 4 advisories | real runtime + dev locks → exit 0 |

`tests/check_gitleaks_gate.py` runs the gitleaks half of that table in CI, before the
clean result is believed.

**It earned its keep on the first CI run.** The planted value was initially 36 repeated `A`s. gitleaks **8.28.0** — the version this repo pins — applies an entropy floor to its `github-pat` rule, so that string scores 0.67 and is ignored; the forge host's own `/usr/bin/gitleaks` (which reports `version is set by build process`) has no such floor and did report it. The probe therefore passed locally while the gate was **inert in CI**, and only the planted side caught it. The value now has real entropy (5.22), is assembled at runtime so this repo never contains a PAT-shaped literal, and the probe prints the gitleaks version so a local/CI skew is visible rather than silent.

### Deployment note

`ecosystem.config.js` is unchanged and `args: "-m signoz_mcp.server"` is still correct,
but the `src/` move **does not survive on the installed package alone**: the venv's
editable install hardcodes the pre-move path, and `import signoz_mcp` raised
`ModuleNotFoundError` after the move until `pip install -e .` was re-run. Deploying this
needs a venv reinstall, not just a PM2 restart.

## [0.3.0] — 2026-07-19

### Breaking

- **`count_errors` removed** — subsumed by
  `aggregate_traces(aggregation="count", filter="has_error = true", group_by="service.name")`.
- **`count_log_errors` removed** — subsumed by
  `aggregate_logs(aggregation="count", filter="severity_text IN ['ERROR', 'WARN']", group_by="resource.service.name")`.
- **`search_traces` signature changed** — was
  `search_traces(service, has_error, min_duration_ms, start, end, limit)` with a
  required `service`; now `search_traces(filter="", service="", operation="",
  has_error=False, min_duration_ms=0, max_duration_ms=0, start, end, limit=100,
  offset=0)`. All params are optional; `filter` accepts a free-form SigNoz filter
  expression AND-combined with the shortcut params. Default `limit` is now 100
  (was 20). Trace field names in generated filters use the v5-canonical dotted
  form (`service.name`, `has_error`, `duration_nano`).

### Added

- **Telemetry + pre/post-hook layer** (forge MCP standard, ported from vikunja-mcp
  v0.2.0). All off by default; the base install gains zero new required deps.
  - `signoz_mcp/telemetry.py` — per-tool-call OTLP spans + metrics
    (`signoz_mcp.tool.calls`/`.errors`/`.latency`), plus best-effort fire-and-forget
    InfluxDB 3 and NATS sinks. Every backend import is lazy/guarded. New env vars
    (all optional): `SIGNOZ_MCP_INFLUXDB3_URL`/`_TOKEN`/`_DATABASE`,
    `SIGNOZ_MCP_NATS_URL`/`_SUBJECT`, and the existing `OTEL_EXPORTER_OTLP_ENDPOINT`
    (now actually wired to metrics + spans). The `SIGNOZ_MCP_` prefix keeps this
    server's telemetry config separate from the upstream `SIGNOZ_*` connection vars.
  - `signoz_mcp/hooks.py` — `register_before`/`register_after` extension-hook
    registry; before-hooks can mutate/abort a call, after-hooks can transform results.
  - `server.instrument`/`server.tool` wrap every tool as
    `run_before_hooks → span/metric → tool body → run_after_hooks`, preserving each
    tool's signature (`wrapper.__signature__`) so FastMCP's schema introspection is
    unchanged.
  - `signoz_mcp/contrib/audit_log.py` — a read-only audit-log before-hook
    (who/what/args-hash, never raw arg values) registered across all tools at startup.
  - New `telemetry` optional-dependency extra (replaces the narrower `otel` extra):
    `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`, `influxdb3-python`,
    `nats-py`.
- `aggregate_traces(...)` / `aggregate_logs(...)` — generic aggregation tools
  (count/count_distinct/avg/sum/min/max/p50–p99/rate) with `group_by`, free-form
  `filter` + shortcut params, and `request_type` `scalar` (default) or
  `time_series`. Scalar responses are parsed from the v5 columns/data table shape.
- `search_logs(...)` — free-form log search (filter + service/severity/search_text
  shortcuts; `search_text` does `body CONTAINS`).
- `get_trace_details(trace_id, start, end, include_spans=True)` — returns every
  span in a trace (`include_spans=True`, via the `raw` request type) or a one-row
  trace summary (`include_spans=False`, via the `trace` request type).

### Security

- Introduced `_FILTER_EXPR_RE`, an expanded filter-expression allowlist for the
  new free-form `filter` params (and unified `query_metric`'s `label_filter` onto
  it). It now permits `- : / @ %` (real service names have dashes; log-body
  filters have slashes) while still excluding `;` `` ` `` `\\` and control
  characters, with a 1000-char cap. The expression is JSON-encoded before
  transport and SigNoz compiles the DSL to ClickHouse server-side (not raw SQL);
  this allowlist is defense-in-depth on a read-only API. Flagged to the security
  agent as the build's one deliberate injection-surface expansion.
- Audit remediation (2026-07-19, LOW): the `operation` shortcut in `search_traces`
  and `aggregate_traces` is now validated with a strict allowlist (`_validate_operation`,
  no quotes) instead of the permissive filter-expression allowlist, so it cannot break
  out of the `name = '<operation>'` string literal it is interpolated into.
- Audit remediation (2026-07-19, INFO): `get_field_keys`/`get_field_values` now validate
  `field_context` and `field_data_type` against their documented allowlists, matching the
  validation applied to the other discovery params.

### Fixed

- **Query-response parsing was broken against live SigNoz v0.118 (SGNZ-8).** The
  v0.2.0 v3→v5 migration updated the request payloads but never validated the
  response parsing against a live instance — the 100%-mocked test suite encoded an
  assumed response shape that did not match reality. The real v5 envelope nests
  aggregation results under `data.data.results[].aggregations[].series[]`, with
  labels as a list of `{"key": {"name": ...}, "value": ...}` objects and values as
  `{"timestamp": ..., "value": ...}` points, and a backend-assigned aggregation
  `alias` (`__result_0`). As a result:
  - `count_errors` and `count_log_errors` filtered on `alias == "error_count"` /
    `"log_error_count"` (never matched) and read `labels`/`values` off the
    aggregation instead of its `series` — so both **silently returned `[]`** against
    real data. Now fixed and verified live.
  - `query_metric` returned raw nested aggregation objects; it now returns clean
    `{labels: {...}, values: [{timestamp, value}]}` series.
  - Note: the SGNZ-8 report's premise (that `/api/v5/query_range` 404s and the fix
    is to switch to `v4`) was incorrect. v5 is the correct, working API on v0.118;
    the observed 404 was a legitimate "could not find the metric" for a metric name
    not ingested on forge, and the empty `count_errors` output was this parsing bug.
- `query_metric`: a 404 for a nonexistent metric now raises a clean
  `ValueError` with SigNoz's own message (e.g. "could not find the metric X")
  instead of a raw `httpx.HTTPStatusError`. `_client` now surfaces SigNoz's
  structured error text for all non-2xx responses without leaking the API key or
  internal URL.
- `search_traces` / `tail_logs`: rows are now unwrapped from the v5
  `{"data": {...}}` per-row envelope before being returned.

### Removed

- `observability.py::get_tracer()` — dead code (was never called). OTEL tracing
  now lives in `telemetry.py`, wired into every tool call.

### Added

- `list_metrics` is functional again. Instead of returning a hardcoded
  "endpoint removed in v0.118" error dict, it now sources metric names and
  metadata from the `GET /api/v2/metrics` endpoint (the same one the official
  SigNoz MCP server uses), with `search_text`, `start`/`end`, `limit`, and
  `source` parameters. Returns metric metadata dicts (`metricName`, `type`,
  `temporality`, `isMonotonic`, ...).
- `get_field_keys(signal, ...)` — discover filterable field keys for a signal
  (`metrics`/`traces`/`logs`) via `GET /api/v1/fields/keys`.
- `get_field_values(signal, name, ...)` — discover values for a specific field
  key via `GET /api/v1/fields/values`.

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
