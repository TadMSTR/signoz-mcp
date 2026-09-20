"""Integration tests that run against a REAL SigNoz backend.

WHY THIS FILE EXISTS. The rest of the suite is 845 lines of respx-mocked tests, and
three shipped defects were invisible to all of it — including vikunja#322, where
`list_services` called an endpoint that silently returned 16 of 25 services. A mock
returns whatever it is told to return, so no mocked test can ever assert that a result
is COMPLETE. That claim is only testable against a live backend.

A SKIP HERE IS NOT A PASS. These skip by default because CI has no SigNoz, and a skipped
test reported alongside passing ones reads as coverage it is not providing. Run them
deliberately:

    SIGNOZ_LIVE=1 SIGNOZ_API_KEY=... pytest tests/test_live_signoz.py -v

Verified this way on forge against SigNoz v0.118.0 on 2026-09-20.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

_LIVE = os.environ.get("SIGNOZ_LIVE") == "1"

requires_live = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "live SigNoz tests are opt-in: set SIGNOZ_LIVE=1 (and SIGNOZ_API_KEY). "
        "A SKIP here is not a PASS — these are the only tests that can detect an "
        "incomplete result, which is what vikunja#322 was."
    ),
)


@pytest.fixture
def server():
    """Import the server module against the live environment."""
    if not os.environ.get("SIGNOZ_API_KEY"):
        pytest.fail("SIGNOZ_LIVE=1 but SIGNOZ_API_KEY is unset — cannot run live.")
    os.environ.setdefault("SIGNOZ_URL", "http://localhost:8080")
    from signoz_mcp import server as s

    return s


# ── vikunja#322 ───────────────────────────────────────────────────────────────


@requires_live
@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["-24h", "-168h"])
async def test_list_services_matches_the_trace_aggregate_as_a_set(server, window):
    """THE regression test for #322, and it asserts SET EQUALITY on purpose.

    A row-count assertion passes on the broken version whenever the counts happen to
    coincide, and a shape assertion cannot express this contract at all. The two
    sources must name the SAME services: if a service emitted a span in the window,
    it must appear in the service list.
    """
    services = await server.list_services(start=window)
    names = {s["serviceName"] for s in services}

    agg = await server.aggregate_traces(
        aggregation="count", group_by="service.name", start=window, limit=1000
    )
    agg_names = {r["service.name"] for r in agg if r.get("service.name")}

    assert names == agg_names, (
        f"list_services and aggregate_traces disagree over {window}.\n"
        f"  only in list_services: {sorted(names - agg_names)}\n"
        f"  only in the aggregate: {sorted(agg_names - names)}\n"
        "This is exactly vikunja#322: the old GET /api/v1/services/list applied its "
        "own short implicit window and under-reported."
    )
    assert names, "no services at all — is SigNoz receiving traces?"


@requires_live
@pytest.mark.asyncio
async def test_list_services_widens_with_the_window(server):
    """A longer window must not return FEWER services.

    The shipped defect returned 16 for every window. This is the cheapest assertion
    that the time range is actually reaching the backend, and it is independent of
    how many services happen to exist today.
    """
    day = {s["serviceName"] for s in await server.list_services(start="-24h")}
    week = {s["serviceName"] for s in await server.list_services(start="-168h")}

    assert day <= week, (
        f"services present at 24h but missing at 7d: {sorted(day - week)} — "
        "a wider window returned a smaller set, so the range is not being applied"
    )


@requires_live
@pytest.mark.asyncio
async def test_list_services_returns_red_metrics(server):
    """The dict return shape, against real data rather than a fixture."""
    services = await server.list_services(start="-168h")
    assert services, "no services returned over 7 days"

    for s in services:
        assert set(s) <= {
            "serviceName",
            "p99",
            "avgDuration",
            "numCalls",
            "callRate",
            "numErrors",
            "errorRate",
            "num4XX",
            "fourXXRate",
        }, f"unexpected keys in {s['serviceName']}: {sorted(s)}"
        assert "dataWarning" not in s
        assert isinstance(s["serviceName"], str) and s["serviceName"]
        assert s["numCalls"] >= 0


# ── vikunja#927 ───────────────────────────────────────────────────────────────


@requires_live
@pytest.mark.asyncio
async def test_tail_logs_takes_no_service_argument(server):
    """#927: the parameter was validated and then discarded.

    Asserted against the live signature rather than by grepping, so it stays true if
    the tool is rewritten.
    """
    import inspect

    sig = inspect.signature(server.tail_logs)
    assert "service" not in sig.parameters, (
        "tail_logs accepts `service` again. If it is now genuinely scoped by it, "
        "prove that against real log data and update this test; vikunja#927 was this "
        "parameter being validated and then silently dropped from the filter."
    )
