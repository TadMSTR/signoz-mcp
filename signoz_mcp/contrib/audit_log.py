"""``before`` hook: audit-log every call to a tool without leaking sensitive values.

Emits one structured log line per call recording *who* (see ``_actor_id``), *what* (the
tool name), and a **hash** of the arguments. Argument values are never logged in the
clear, so a filter expression or free-text search term does not spill into the audit
trail — only a digest that lets you correlate identical calls.

signoz-mcp is entirely read-only, but it holds a real ``SIGNOZ_API_KEY`` and its tools
touch potentially sensitive service/trace/log data, so auditing *that* a query happened
(and correlating repeats) is worth having. ``server.main`` registers this across all
tools at startup.

Register it explicitly for a subset if you prefer::

    from signoz_mcp.contrib.audit_log import register_audit_log

    register_audit_log(["search_traces", "search_logs", "tail_logs"])

To route the line into ``~/.claude/comms/artifacts/tool-audit/`` instead of stdout, pass
your own ``logger`` (any object with an ``info(event, **fields)`` method).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from typing import Any

import structlog

from ..hooks import register_before

_default_log = structlog.get_logger("signoz_mcp.audit")


def _digest(value: Any) -> str:
    """Stable short SHA-256 digest of a JSON-serialisable value."""
    blob = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _actor_id() -> str:
    """Identifier for the calling agent.

    signoz-mcp is a single-tenant internal tool authenticated by one shared
    service-account key (``SIGNOZ_API_KEY``) rather than a per-caller bearer token, so
    there is no per-caller identity to derive — this returns ``"anonymous"``. It is kept
    as a function so a future per-caller auth layer can hash a caller token here (as
    vikunja-mcp does) without touching the hook contract.
    """
    return "anonymous"


def audit_log_hook(tool: str, logger: Any | None = None) -> Callable[[dict], Any]:
    """Build a ``before`` handler that audit-logs calls to ``tool``.

    Args:
        tool: the tool name this handler will be registered for (logged verbatim).
        logger: optional sink with an ``info(event, **fields)`` method; defaults to a
            structlog logger. The kwargs are hashed, never logged raw.
    """
    sink = logger or _default_log

    async def handler(kwargs: dict) -> dict:
        sink.info(
            "signoz_tool_call",
            tool=tool,
            actor=_actor_id(),
            args_hash=_digest(kwargs),
        )
        return kwargs

    return handler


def register_audit_log(tools: Iterable[str], logger: Any | None = None) -> None:
    """Register the audit-log hook for each tool name in ``tools``."""
    for tool in tools:
        register_before(tool, audit_log_hook(tool, logger=logger))
