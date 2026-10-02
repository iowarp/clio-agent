"""Expiry of a permission request nobody answered: typed, published and logged (#1577).

The tool gate (:mod:`clio_agent.gact.permission_gate`) blocks a non-read call on the
user's answer for :data:`PERMISSION_REQUEST_TIMEOUT_S`. When that passes with no answer the
call is denied -- fail-safe -- but never silently: :func:`expire_permission` resolves the row
with the typed reason :data:`REASON_PERMISSION_TIMEOUT` (the tool-gate twin of
``grants.REASON_EGRESS_TIMEOUT``), publishes ``permission.resolved`` carrying it, logs a
WARNING, and returns a deny whose message tells the model the request timed out, which is
not the same as the user refusing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.gact.permission_delivery import publish_permission_event

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.permission_gate import DenyDecision

logger = logging.getLogger(__name__)

#: How long a tool call waits for the user to answer its permission request.
PERMISSION_REQUEST_TIMEOUT_S = 600.0
#: The typed reason on a permission request that expired unanswered.
REASON_PERMISSION_TIMEOUT = "permission_request_timeout"


def expire_permission(
    app: "FastAPI", pid: str, row: dict[str, Any], *, tool_name: str
) -> "DenyDecision | None":
    """Resolve an unanswered request as a typed timeout deny; ``None`` if it was answered.

    An answer that landed between the wait expiring and this call wins: the row is no
    longer pending, nothing is rewritten, and the caller reads the user's action.
    """
    from clio_agent.gact.permission_gate import DenyDecision  # noqa: PLC0415 - import cycle

    if row.get("status") != "pending":
        return None
    waited = PERMISSION_REQUEST_TIMEOUT_S
    row["status"] = "timeout"
    row["action"] = "deny"
    row["reason"] = REASON_PERMISSION_TIMEOUT
    row["resolved_at"] = datetime.now(timezone.utc).isoformat()
    app.state.permission_events.pop(pid, None)
    session_id = str(row.get("session_id") or "")
    logger.warning(
        "permission request expired unanswered reason=%s permission_id=%s tool=%s "
        "session=%s waited_s=%.0f",
        REASON_PERMISSION_TIMEOUT,
        pid,
        tool_name,
        session_id,
        waited,
    )
    publish_permission_event(
        app,
        "permission.resolved",
        owner_session_id=session_id,
        payload={
            "permission_id": pid,
            "action": "deny",
            "session_id": session_id,
            "reason": REASON_PERMISSION_TIMEOUT,
        },
    )
    return DenyDecision(
        f"The permission request for {tool_name!r} timed out after {waited:.0f}s with no "
        "answer from the user, so the call was not run. This is not a refusal: ask the "
        "user before trying it again."
    )


__all__ = ["PERMISSION_REQUEST_TIMEOUT_S", "REASON_PERMISSION_TIMEOUT", "expire_permission"]
