"""Tests for signoz_mcp/telemetry.py — disabled path and the _emit fan-out.

The base/[dev] install carries none of the telemetry backends, so these tests exercise
the no-op path and mock the OTLP/Influx sinks rather than requiring the optional deps.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from signoz_mcp import telemetry


@pytest.fixture(autouse=True)
def _reset():
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


@pytest.mark.asyncio
async def test_record_tool_call_transparent_when_disabled():
    ran = False
    async with telemetry.record_tool_call("t"):
        ran = True
    assert ran


@pytest.mark.asyncio
async def test_record_tool_call_reraises_and_emits(monkeypatch):
    seen = []
    monkeypatch.setattr(telemetry, "_emit", lambda tool, dur, err: seen.append((tool, err)))
    with pytest.raises(RuntimeError):
        async with telemetry.record_tool_call("t"):
            raise RuntimeError("boom")
    assert seen and seen[0] == ("t", "RuntimeError")


def test_init_idempotent_and_noop_without_env(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("SIGNOZ_MCP_INFLUXDB3_URL", raising=False)
    telemetry.init()
    telemetry.init()  # second call short-circuits on _initialized
    assert telemetry._tracer is None
    assert telemetry._influx_client is None
    assert telemetry._initialized is True


def test_emit_no_backends_does_not_raise(monkeypatch):
    monkeypatch.delenv("SIGNOZ_MCP_NATS_URL", raising=False)
    telemetry._emit("t", 0.1, None)  # no counters, no sinks — must be a clean no-op


def test_emit_records_to_otlp_instruments(monkeypatch):
    calls, errors, latency = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(telemetry, "_calls_counter", calls)
    monkeypatch.setattr(telemetry, "_errors_counter", errors)
    monkeypatch.setattr(telemetry, "_latency_hist", latency)
    monkeypatch.delenv("SIGNOZ_MCP_NATS_URL", raising=False)

    telemetry._emit("t", 0.2, "BoomError")
    calls.add.assert_called_once()
    latency.record.assert_called_once()
    errors.add.assert_called_once()  # only on error


def test_emit_no_error_counter_when_ok(monkeypatch):
    calls, errors, latency = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(telemetry, "_calls_counter", calls)
    monkeypatch.setattr(telemetry, "_errors_counter", errors)
    monkeypatch.setattr(telemetry, "_latency_hist", latency)
    monkeypatch.delenv("SIGNOZ_MCP_NATS_URL", raising=False)

    telemetry._emit("t", 0.2, None)
    calls.add.assert_called_once()
    errors.add.assert_not_called()


def test_influx_write_is_best_effort(monkeypatch):
    client = MagicMock()
    client.write.side_effect = RuntimeError("influx down")
    monkeypatch.setattr(telemetry, "_influx_client", client)
    # Must swallow the write error (and any missing-dep ImportError) — never propagate.
    telemetry._influx_write("t", 0.1, None)


@pytest.mark.asyncio
async def test_nats_publish_short_circuits_without_url(monkeypatch):
    monkeypatch.delenv("SIGNOZ_MCP_NATS_URL", raising=False)
    await telemetry._nats_publish("t", 0.1, None)
    assert telemetry._nats_conn is None


def test_schedule_without_loop_is_noop():
    async def _c():
        return None

    # No running loop → _schedule must close the coroutine and not raise.
    telemetry._schedule(_c())


class _SpanCM:
    def __init__(self):
        self.span = MagicMock()

    def __enter__(self):
        return self.span

    def __exit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_record_tool_call_opens_span_and_records_exception(monkeypatch):
    tracer = MagicMock()
    scm = _SpanCM()
    tracer.start_as_current_span.return_value = scm
    monkeypatch.setattr(telemetry, "_tracer", tracer)
    monkeypatch.delenv("SIGNOZ_MCP_NATS_URL", raising=False)

    # success path opens the span
    async with telemetry.record_tool_call("ok"):
        pass
    tracer.start_as_current_span.assert_called_with("tool.ok")

    # error path records the exception on the span, then re-raises
    with pytest.raises(ValueError):
        async with telemetry.record_tool_call("boom"):
            raise ValueError("x")
    scm.span.record_exception.assert_called()


@pytest.mark.asyncio
async def test_schedule_with_running_loop_runs_task():
    ran = []

    async def c():
        ran.append(1)

    telemetry._schedule(c())
    assert telemetry._bg_tasks, "task must be retained (strong ref) while in flight"
    await asyncio.sleep(0)  # let the scheduled task run to completion
    assert ran == [1]


@pytest.mark.asyncio
async def test_aclose_closes_backends():
    nats = MagicMock()
    nats.drain = AsyncMock()
    influx = MagicMock()
    telemetry._nats_conn = nats
    telemetry._influx_client = influx
    telemetry._initialized = True

    await telemetry.aclose()

    nats.drain.assert_awaited_once()
    influx.close.assert_called_once()
    assert telemetry._nats_conn is None
    assert telemetry._influx_client is None
    assert telemetry._initialized is False
