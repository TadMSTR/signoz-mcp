# Contributing

## How to add a tool

1. Add a new `@mcp.tool()` function in `signoz_mcp/server.py`
   - Keep tools read-only — signoz-mcp is a strictly read/query interface
   - Use the shared `SigNozClient` from `_client.py`
   - Validate all inputs before passing to the API (see `_METRIC_NAME_RE` for the validation pattern)
   - Return structured dicts, not raw API responses

2. Add tests in `tests/test_server.py` using `respx` to mock the SigNoz HTTP API
   - Every tool needs at minimum a happy-path test
   - Add a test for invalid inputs that should raise `ValueError`

3. Update the tool table in `README.md`

4. Add a `CHANGELOG.md` entry under `[Unreleased]`

## Testing requirements

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=signoz_mcp --cov-report=term-missing
```

Coverage threshold: 80% (`observability.py` and `__main__.py` are excluded — infrastructure boilerplate).

All tests must pass before opening a PR. CI runs the full matrix (Python 3.11–3.14).

## Code style

```bash
# Lint
ruff check .

# Format check
ruff format --check .

# Format (apply)
ruff format .
```

Line length: 100 characters. Target: Python 3.11+.

## Comment policy

Comments exist to prevent future changes from breaking security or correctness invariants — not to explain what the code does.

Write a comment when:
- You're enforcing an input validation rule and the reason isn't obvious from the code
- You're explaining why a simpler-looking alternative would be wrong or insecure
- You're documenting a constraint imposed by the SigNoz API (e.g., field renames across API versions)

Do not write comments that restate what the code does (`# validate metric name`).

## What not to change without discussion

- The `_METRIC_NAME_RE` validation pattern and `_MAX_LABEL_FILTER_LEN` cap — these are security controls
- The `SIGNOZ_QUERY_VERSION` default — changing this silently breaks deployed instances
- The read-only contract — signoz-mcp must never write to or modify SigNoz state

## PR process

1. Fork and create a feature branch (`git checkout -b feat/my-tool`)
2. Write tests first
3. Implement the tool
4. Run `pytest`, `ruff check .`, and `ruff format --check .`
5. Open a PR against `main`
6. CI must pass before merge
