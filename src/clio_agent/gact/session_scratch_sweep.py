"""Sweep stale per-session scratch folders once at server start (TD56).

A deleted session's scratch folder goes with it
(:func:`clio_agent.gact.session_tool_output.delete_session_tool_output`). This sweep
removes the rest: this machine's folders whose session no longer exists, or whose
session's last activity (``last_interaction_at``, else ``created_at``) is older than
``paths.scratch_retention_days``. It runs on a worker thread, because session reads wait
for the clio-core attach; a failure is logged with a typed reason and removes nothing more.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.runtime import session_scratch, trace

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Typed reason: the boot sweep stopped (the session store was not readable).
SWEEP_FAILED_REASON = "session_scratch_sweep_failed"
#: Typed reason: a session's activity timestamp is unreadable; its folder is kept.
ACTIVITY_UNREADABLE_REASON = "session_scratch_activity_unreadable"


def _epoch(stamp: str) -> float | None:
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def last_activity(sessions: Any, session_id: str) -> float | None:
    """The session's last activity in epoch seconds; ``None`` when it no longer exists.

    An unreadable timestamp counts as activity now, so its folder is kept (recorded with
    :data:`ACTIVITY_UNREADABLE_REASON`) rather than removed on a guess.
    """

    session = sessions.get(session_id)
    if session is None:
        return None
    stamp = str(getattr(session, "last_interaction_at", "") or getattr(session, "created_at", ""))
    when = _epoch(stamp)
    if when is None:
        trace.event(
            "SCRATCH", "%s session=%s stamp=%r", ACTIVITY_UNREADABLE_REASON, session_id, stamp
        )
        return float("inf")
    return when


def sweep_sessions(sessions: Any) -> list[str]:
    """Sweep against ``sessions`` (the session store); return the removed session ids."""

    try:
        return session_scratch.sweep(lambda sid: last_activity(sessions, sid))
    except Exception as exc:  # noqa: BLE001 - reported with a typed reason, retried next boot
        logger.warning("session scratch sweep stopped reason=%s error=%r", SWEEP_FAILED_REASON, exc)
        trace.event("SCRATCH", "%s error=%r", SWEEP_FAILED_REASON, exc)
        return []


def start(app: "FastAPI") -> None:
    """Run the sweep once on a worker thread (the real server's boot)."""

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return
    app.state.session_scratch_sweep = asyncio.get_running_loop().create_task(
        asyncio.to_thread(sweep_sessions, sessions), name="clio-session-scratch-sweep"
    )


__all__ = ["ACTIVITY_UNREADABLE_REASON", "SWEEP_FAILED_REASON", "last_activity", "start"]
