"""Typed, non-silent skip-reason ledger for the MCP Apps observer (#1308).

``gact/mcp_apps.py``'s auto-firing observer (``_make_mcp_app_observer``) has
three early-return gates -- no declared ``resourceUri``, an ``isError``
result, no resolvable session -- that used to drop a ui-bearing tool result
with ZERO trace. The live #1308 symptom (a real session turn's ui-bearing
tool call succeeded, the turn went idle, and no ``mcp_app`` Part landed) was
undiagnosable for exactly that reason.

This is a NEW owner module (no-accretion: ``mcp_apps.py`` is already at its
file-size ratchet baseline -- see the ``#947 DEBT`` block in
``scripts/check_file_size.py``) mirroring ``tools.execution``'s
``_TOOL_RUNTIME_REASON_DEFINITIONS`` / ``_emit_tool_runtime_reason``
reason-catalog style: a typed definition, a ``stream_audit`` JSONL row, and
a bounded in-process ring queryable after the fact
(:func:`recorded_mcp_app_observer_skips`) -- the same
reason-reaches-the-trace contract the ``stream_fallback`` catalog
(``gact/streaming.py``) established for degraded live-delivery paths.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)


_MCP_APP_OBSERVER_SKIP_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "mcp_app_skipped_no_resource_uri": {
        "severity": "info",
        "detail": (
            "the mounted tool definition carries no ui://-scheme resourceUri in "
            "its _meta.ui -- either the tool was never declared as an MCP App, "
            "or the discovered definition reaching this call is stale/incomplete"
        ),
    },
    "mcp_app_skipped_error_result": {
        "severity": "info",
        "detail": (
            "the tool call's result carried isError=true; no App is registered for a failed call"
        ),
    },
    "mcp_app_skipped_no_session": {
        "severity": "warning",
        "detail": (
            "no active or recent session resolved for this tool call; the App "
            "result cannot be attributed to a session and is dropped"
        ),
    },
}

_MCP_APP_OBSERVER_SKIPS: "deque[dict[str, Any]]" = deque(maxlen=256)
_MCP_APP_OBSERVER_SKIPS_LOCK = threading.Lock()


def record_observer_skip(reason: str, **fields: Any) -> dict[str, Any]:
    """Record a typed reason for a dropped ui-bearing tool result (#1308).

    Every early return in ``mcp_apps._make_mcp_app_observer``'s ``observe``
    calls this instead of a bare ``return`` -- the degradation reaches a
    logger warning, the ``stream_audit`` JSONL sink, and a bounded queryable
    in-process ring, never silently.
    """

    definition = _MCP_APP_OBSERVER_SKIP_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown MCP App observer skip reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, **definition, **fields}
    with _MCP_APP_OBSERVER_SKIPS_LOCK:
        _MCP_APP_OBSERVER_SKIPS.append(payload)
    stream_audit("mcp_app_observer_skip", **payload)
    logger.warning(
        "MCP App result dropped tool=%s reason=%s detail=%s",
        fields.get("tool", ""),
        reason,
        definition["detail"],
    )
    return payload


def recorded_mcp_app_observer_skips() -> list[dict[str, Any]]:
    """Return a snapshot of recorded MCP-App observer skip reasons (queryable audit)."""

    with _MCP_APP_OBSERVER_SKIPS_LOCK:
        return list(_MCP_APP_OBSERVER_SKIPS)


__all__ = [
    "record_observer_skip",
    "recorded_mcp_app_observer_skips",
]
