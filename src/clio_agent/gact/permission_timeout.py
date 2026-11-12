"""Waiting for a person's answer to a permission request (#1577; taskable run_code CT13).

A question waits until the person answers it or its work is cancelled. Nothing expires it
by default: CLIO is a human-observed agent, and a person who steps away for lunch, or
answers an overnight run's question in the morning, must come back to the question, not
to a denied call. While it waits, the wait counts as the turn's progress
(:func:`clio_agent.runtime.commitment_activity.track`), so the turn's no-progress watchdog
does not cancel a turn that is only waiting on the person.

A caller that is given a deadline explicitly (the PreToolUse hook defer,
``hooks.defer_timeout``) still gets one: when it passes, :func:`expire_permission` resolves
the row with the typed reason :data:`REASON_PERMISSION_TIMEOUT`, publishes
``permission.resolved`` carrying it, logs a WARNING, and returns a deny whose message says
the request went unanswered, which is not the same as the person refusing.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from clio_agent.gact.permission_delivery import publish_permission_event
from clio_agent.gact.permission_evidence import emit_permission_resolution

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.permission_gate import DenyDecision

logger = logging.getLogger(__name__)

#: The typed reason on a permission request that expired unanswered.
REASON_PERMISSION_TIMEOUT = "permission_request_timeout"
REASON_PERMISSION_CANCELLED = "permission_request_cancelled"


def wait_for_permission(
    app: FastAPI,
    pid: str,
    row: dict[str, Any],
    event: threading.Event,
    *,
    tool_name: str,
    cancelled: threading.Event | None,
    timeout_s: float | None = None,
) -> DenyDecision | None:
    """Await the person's answer, retaining the original turn's cancellation token.

    No deadline unless ``timeout_s`` is given (module doc). Stop ends this invocation's
    approval wait, not an accepted background task. A cancellation resolves only a
    still-pending request; it cannot rewrite a human decision or grant access.
    """
    from clio_agent.runtime.commitment_activity import track  # noqa: PLC0415

    session_id = str(row.get("session_id") or "") or None
    with track(True, session_id=session_id):
        return _wait(app, pid, row, event, tool_name=tool_name, cancelled=cancelled,
                     timeout_s=timeout_s)  # fmt: skip


def _wait(
    app: FastAPI,
    pid: str,
    row: dict[str, Any],
    event: threading.Event,
    *,
    tool_name: str,
    cancelled: threading.Event | None,
    timeout_s: float | None,
) -> DenyDecision | None:
    from clio_agent.gact.folder_access import FOLDER_RESOLUTION_LOCK
    from clio_agent.gact.permission_gate import DenyDecision, resolve_permission

    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while True:
        if cancelled is not None and cancelled.is_set():
            with FOLDER_RESOLUTION_LOCK:
                if row.get("status") == "pending":
                    row["reason"] = REASON_PERMISSION_CANCELLED
                    resolve_permission(app, pid, "deny", grantor="system")
            decision = DenyDecision(
                f"The invocation of {tool_name!r} was cancelled before approval."
            )
            # Nobody refused it: the turn stopped (or the server shut down) while it
            # waited. The tool layer records the call as cancelled, not denied.
            cast(Any, decision).outcome = "cancelled"  # read by tool_observation
            return decision
        if event.is_set():
            return None
        if deadline is None:
            event.wait(0.1)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with FOLDER_RESOLUTION_LOCK:
                return expire_permission(app, pid, row, tool_name=tool_name, waited_s=timeout_s)
        event.wait(min(0.1, remaining))


def expire_permission(
    app: "FastAPI", pid: str, row: dict[str, Any], *, tool_name: str, waited_s: float | None = None
) -> "DenyDecision | None":
    """Resolve an unanswered request as a typed timeout deny; ``None`` if it was answered.

    An answer that landed between the wait expiring and this call wins: the row is no
    longer pending, nothing is rewritten, and the caller reads the user's action.
    """
    from clio_agent.gact.permission_gate import DenyDecision  # noqa: PLC0415 - import cycle

    if row.get("status") != "pending":
        return None
    row["status"] = "timeout"
    row["action"] = "deny"
    row["reason"] = REASON_PERMISSION_TIMEOUT
    row["resolved_at"] = datetime.now(timezone.utc).isoformat()
    row["grantor"] = "system"
    app.state.permission_events.pop(pid, None)
    session_id = str(row.get("session_id") or "")
    logger.warning(
        "permission request expired unanswered reason=%s permission_id=%s tool=%s "
        "session=%s waited_s=%s",
        REASON_PERMISSION_TIMEOUT,
        pid,
        tool_name,
        session_id,
        "unknown" if waited_s is None else f"{waited_s:.0f}",
    )
    emit_permission_resolution(app, row)
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
    after = "" if waited_s is None else f" after {waited_s:.0f}s"
    return DenyDecision(
        f"The permission request for {tool_name!r} timed out{after} with no "
        "answer from the user, so the call was not run. This is not a refusal: ask the "
        "user before trying it again."
    )


__all__ = [
    "REASON_PERMISSION_CANCELLED",
    "REASON_PERMISSION_TIMEOUT",
    "expire_permission",
    "wait_for_permission",
]
