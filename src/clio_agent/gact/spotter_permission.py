"""SPOTTER clearance enforcement at the mutating-tool permission boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.spotter_clearance import (
    SPOTTER_CLEARANCE_REASONS,
    wait_for_spotter_clearance,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


def enforce_spotter_clearance(
    app: "FastAPI",
    sid: str,
    session: Any,
    tool_name: str,
    args: Mapping[str, Any],
    subject: str,
    record_resolution: Callable[..., Any],
) -> str:
    """Wait for standing surveillance or return a fail-closed denial message.

    The barrier's own typed outcome (a crashed watcher, a watcher that is not
    running at all, or one that stopped making observable progress — see
    :data:`~clio_agent.gact.spotter_clearance.SPOTTER_CLEARANCE_REASONS`) is
    carried through to BOTH the permission audit row's ``reason`` field and the
    model-facing denial text, so the two never disagree about why a containment
    denial happened.

    Args:
        app: The GACT app.
        sid: The session driving the tool call.
        session: That session's record (``None`` / non-spotter modes clear).
        tool_name: The tool being gated.
        args: The tool's arguments, recorded verbatim on the audit row.
        subject: The gate's noun for the call ("destructive tool", ...).
        record_resolution: The gate's audit-row writer
            (``permission_gate._record_resolved_permission``).

    Returns:
        ``""`` when the call may proceed, else the model-facing denial message.
    """

    if session is None or getattr(session, "approval_mode", "") != "spotter-ai":
        return ""
    reason = wait_for_spotter_clearance(app, sid)
    if not reason:
        return ""
    message = SPOTTER_CLEARANCE_REASONS[reason]
    record_resolution(
        app,
        session_id=sid,
        tool_name=tool_name,
        args=args,
        status="auto_denied",
        action="deny",
        summary=f"{subject} {tool_name!r} blocked by SPOTTER containment: {message}",
        reason=reason,
    )
    return message
