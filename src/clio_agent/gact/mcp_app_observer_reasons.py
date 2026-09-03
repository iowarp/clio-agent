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

Severity + ring split (Opus review, #1308 F1): the observer runs after
EVERY successful MCP tool call, not just App ones, so
``mcp_app_skipped_no_resource_uri`` is the OVERWHELMING majority reason (an
ordinary, non-App tool call -- not a real degradation) while
``mcp_app_skipped_no_session`` is the rare diagnostic signal #1308 actually
needed. Logging every reason at ``WARNING`` flooded logs with a misleading
"dropped" line for the common case; recording every reason into ONE bounded
ring let that same flood evict the rare row before anyone could query it.
Two fixes: (1) logging is routed by the reason's OWN declared ``severity``
(``info`` -> ``logger.info``, never ``logger.warning``, and its message never
claims something was "dropped" -- nothing was, this is the normal path);
(2) the ring is SPLIT so the high-volume ``no_resource_uri`` reason has its
own bounded ring and can never evict a rare row from the other one.
:func:`recorded_mcp_app_observer_skips` still returns BOTH, merged, so a
caller never needs to know about the split.
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

#: A bespoke log-message TEMPLATE for a reason whose generic "dropped" wording
#: would mislead (Opus review, #1308 F1) -- ``no_resource_uri`` is the NORMAL
#: shape of an ordinary, non-App tool call, not a degradation. Any reason
#: without an entry here falls back to ``_DEFAULT_SKIP_LOG_TEMPLATE``.
_MCP_APP_OBSERVER_SKIP_LOG_TEMPLATES: dict[str, str] = {
    "mcp_app_skipped_no_resource_uri": (
        "MCP App observer: tool=%s call declares no App resourceUri (the "
        "ordinary, non-App shape) reason=%s detail=%s"
    ),
}
_DEFAULT_SKIP_LOG_TEMPLATE = "MCP App result dropped tool=%s reason=%s detail=%s"

#: The RARE-reason ring (``error_result`` / ``no_session``) -- #1308's actual
#: diagnostic signal. Kept separate from the high-volume ring below so a
#: burst of ordinary skips can never evict a row here (Opus review F1).
_MCP_APP_OBSERVER_SKIPS: "deque[dict[str, Any]]" = deque(maxlen=256)
#: The HIGH-VOLUME ``no_resource_uri`` ring -- every ordinary (non-App) tool
#: call lands here instead, insulating the ring above.
_NO_RESOURCE_URI_SKIPS: "deque[dict[str, Any]]" = deque(maxlen=256)
_MCP_APP_OBSERVER_SKIPS_LOCK = threading.Lock()


def record_observer_skip(reason: str, **fields: Any) -> dict[str, Any]:
    """Record a typed reason for a dropped ui-bearing tool result (#1308).

    Every early return in ``mcp_apps._make_mcp_app_observer``'s ``observe``
    calls this instead of a bare ``return`` -- the degradation reaches a
    severity-routed logger call (see module docstring), the ``stream_audit``
    JSONL sink, and a bounded queryable in-process ring, never silently.
    """

    definition = _MCP_APP_OBSERVER_SKIP_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown MCP App observer skip reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, **definition, **fields}
    ring = (
        _NO_RESOURCE_URI_SKIPS
        if reason == "mcp_app_skipped_no_resource_uri"
        else _MCP_APP_OBSERVER_SKIPS
    )
    with _MCP_APP_OBSERVER_SKIPS_LOCK:
        ring.append(payload)
    stream_audit("mcp_app_observer_skip", **payload)
    log = logger.warning if definition["severity"] == "warning" else logger.info
    template = _MCP_APP_OBSERVER_SKIP_LOG_TEMPLATES.get(reason, _DEFAULT_SKIP_LOG_TEMPLATE)
    log(template, fields.get("tool", ""), reason, definition["detail"])
    return payload


def recorded_mcp_app_observer_skips() -> list[dict[str, Any]]:
    """Return a snapshot of recorded MCP-App observer skip reasons (queryable audit).

    Merges BOTH rings (the rare-reason ring and the high-volume
    ``no_resource_uri`` ring, see module docstring) -- a caller never needs
    to know the split exists.
    """

    with _MCP_APP_OBSERVER_SKIPS_LOCK:
        return list(_MCP_APP_OBSERVER_SKIPS) + list(_NO_RESOURCE_URI_SKIPS)


__all__ = [
    "record_observer_skip",
    "recorded_mcp_app_observer_skips",
]
