"""Tests for signoz_mcp/hooks.py and the server.instrument hook wiring."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from signoz_mcp.hooks import (
    clear_hooks,
    register_after,
    register_before,
    run_after_hooks,
    run_before_hooks,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SIGNOZ_API_KEY", "test-api-key")
    monkeypatch.setenv("SIGNOZ_URL", "http://localhost:8080")
    monkeypatch.setenv("SIGNOZ_QUERY_VERSION", "v5")


@pytest.fixture(autouse=True)
def _clear_hooks():
    clear_hooks()
    yield
    clear_hooks()


# ── registry unit behavior ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_before_hooks_run_in_registration_order():
    order = []

    async def h1(kwargs):
        order.append(1)
        return kwargs

    async def h2(kwargs):
        order.append(2)
        return kwargs

    register_before("t", h1)
    register_before("t", h2)
    await run_before_hooks("t", {})
    assert order == [1, 2]


@pytest.mark.asyncio
async def test_before_hook_mutates_kwargs():
    async def add(kwargs):
        kwargs["added"] = True
        return kwargs

    register_before("t", add)
    out = await run_before_hooks("t", {"x": 1})
    assert out == {"x": 1, "added": True}


@pytest.mark.asyncio
async def test_after_hook_transforms_result():
    async def wrap(result):
        return {"wrapped": result}

    register_after("t", wrap)
    out = await run_after_hooks("t", [1, 2])
    assert out == {"wrapped": [1, 2]}


@pytest.mark.asyncio
async def test_hook_exception_propagates():
    async def boom(kwargs):
        raise RuntimeError("nope")

    register_before("t", boom)
    with pytest.raises(RuntimeError):
        await run_before_hooks("t", {})


@pytest.mark.asyncio
async def test_no_hooks_passthrough():
    assert await run_before_hooks("none", {"a": 1}) == {"a": 1}
    assert await run_after_hooks("none", "r") == "r"


# ── wiring through a real instrumented server tool ────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_before_hook_fires_on_instrumented_tool():
    respx.get("http://localhost:8080/api/v1/services/list").mock(
        return_value=Response(200, json=["svc"])
    )
    fired = {}

    async def h(kwargs):
        fired["yes"] = True
        return kwargs

    register_before("list_services", h)
    from signoz_mcp.server import list_services

    await list_services()
    assert fired.get("yes")


@pytest.mark.asyncio
@respx.mock
async def test_after_hook_transforms_tool_result():
    respx.get("http://localhost:8080/api/v1/services/list").mock(
        return_value=Response(200, json=["svc"])
    )

    async def wrap(result):
        return {"wrapped": result}

    register_after("list_services", wrap)
    from signoz_mcp.server import list_services

    result = await list_services()
    assert result == {"wrapped": ["svc"]}


@pytest.mark.asyncio
@respx.mock
async def test_before_hook_mutation_reaches_upstream_call():
    captured = []

    def capture(request):
        captured.append(request)
        return Response(200, json={"data": {"metrics": []}})

    respx.get("http://localhost:8080/api/v2/metrics").mock(side_effect=capture)

    async def inject(kwargs):
        kwargs["search_text"] = "cpu"
        return kwargs

    register_before("list_metrics", inject)
    from signoz_mcp.server import list_metrics

    await list_metrics()
    assert "searchText=cpu" in str(captured[0].url)


@pytest.mark.asyncio
@respx.mock
async def test_before_hook_abort_prevents_upstream_call():
    route = respx.get("http://localhost:8080/api/v1/services/list").mock(
        return_value=Response(200, json=["svc"])
    )

    async def boom(kwargs):
        raise RuntimeError("blocked")

    register_before("list_services", boom)
    from signoz_mcp.server import list_services

    with pytest.raises(RuntimeError):
        await list_services()
    assert not route.called, "upstream must not be called when a before-hook aborts"


def test_instrumented_tool_preserves_signature():
    """The instrument wrapper must keep the tool's real signature for FastMCP schema."""
    import inspect

    from signoz_mcp.server import get_field_values, instrument

    async def sample(signal: str, name: str, limit: int = 5):
        return None

    wrapped = instrument(sample)
    params = list(inspect.signature(wrapped).parameters)
    assert params == ["signal", "name", "limit"]
    # And the registered tool exposes its params too (not (*args, **kwargs)).
    assert "signal" in str(inspect.signature(get_field_values))
