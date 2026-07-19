"""Tests for signoz_mcp/contrib/audit_log.py."""

from __future__ import annotations

import pytest

from signoz_mcp.contrib.audit_log import (
    _actor_id,
    _digest,
    audit_log_hook,
    register_audit_log,
)
from signoz_mcp.hooks import _before, clear_hooks


@pytest.fixture(autouse=True)
def _clear_hooks():
    clear_hooks()
    yield
    clear_hooks()


class _CaptureLogger:
    def __init__(self):
        self.lines: list[tuple[str, dict]] = []

    def info(self, event, **fields):
        self.lines.append((event, fields))


@pytest.mark.asyncio
async def test_audit_hook_logs_tool_and_hash_not_raw_values():
    cap = _CaptureLogger()
    handler = audit_log_hook("search_traces", logger=cap)

    kwargs = {"filter": "service.name = 'super-secret-svc'", "service": "svc"}
    out = await handler(kwargs)

    assert out == kwargs  # passthrough — before hooks return kwargs
    event, fields = cap.lines[0]
    assert event == "signoz_tool_call"
    assert fields["tool"] == "search_traces"
    assert len(fields["args_hash"]) == 16
    # Raw argument values must never appear in the audit line.
    assert "super-secret-svc" not in str(fields)


def test_actor_id_is_anonymous():
    assert _actor_id() == "anonymous"


def test_digest_is_stable_and_short():
    a = _digest({"x": 1, "y": 2})
    b = _digest({"y": 2, "x": 1})  # key order independent
    assert a == b
    assert len(a) == 16


def test_register_audit_log_wires_before_hooks():
    register_audit_log(["get_health", "list_services"])
    assert "get_health" in _before
    assert "list_services" in _before
