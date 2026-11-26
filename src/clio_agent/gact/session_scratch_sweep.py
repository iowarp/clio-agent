"""Sweep stale per-session scratch folders once at server start (TD56).

A deleted session's scratch folder goes with it
(:func:`clio_agent.gact.session_tool_output.delete_session_tool_output`). This sweep
removes the rest: this machine's folders whose session no longer exists, or whose
session's last activity (``last_interaction_at``, else ``created_at``) is older than
``paths.scratch_retention_days``. It waits for the session store on the server loop
(cancelled with the server) and then runs on a worker thread: a worker blocked on the
clio-core attach held the server's shutdown, which joins its threads. A failure is logged
with a typed reason and removes nothing more. The short roots that stood in for deleted
homes and sessions go next (``runtime.short_root_sweep``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.gact.transcript_file import TranscriptNotReadyError
from clio_agent.runtime import session_scratch, trace

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Typed reason: the boot sweep stopped (the session store was not readable).
SWEEP_FAILED_REASON = "session_scratch_sweep_failed"
#: Typed reason: the session store did not attach in time; nothing was swept this boot.
STORE_NOT_READY_REASON = "session_scratch_store_not_ready"
#: Seconds the sweep waits for the session store, and how often it looks.
_READY_WAIT_S = 300.0
_READY_POLL_S = 0.5
#: Typed reason: clio-core's metadata restore failed this boot; no folder is swept.
RESTORE_SUSPECT_REASON = "session_scratch_store_restore_suspect"
#: Typed reason: a session in the index whose record does not read back; its folder is kept.
RECORD_UNREADABLE_REASON = "session_scratch_record_unreadable"
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


def last_activity(
    sessions: Any, session_id: str, indexed: "frozenset[str]" = frozenset()
) -> float | None:
    """The session's last activity in epoch seconds; ``None`` when it no longer exists.

    An unreadable timestamp counts as activity now, so its folder is kept (recorded with
    :data:`ACTIVITY_UNREADABLE_REASON`) rather than removed on a guess. So does a
    session still in the index (``indexed``) whose record does not read back
    (:data:`RECORD_UNREADABLE_REASON`): it exists, its record is damaged (FN-14).
    """

    session = sessions.get(session_id)
    if session is None:
        if session_id not in indexed:
            return None
        logger.warning(
            "session scratch kept reason=%s session=%s", RECORD_UNREADABLE_REASON, session_id
        )
        trace.event("SCRATCH", "%s session=%s", RECORD_UNREADABLE_REASON, session_id)
        return float("inf")
    stamp = str(getattr(session, "last_interaction_at", "") or getattr(session, "created_at", ""))
    when = _epoch(stamp)
    if when is None:
        trace.event(
            "SCRATCH", "%s session=%s stamp=%r", ACTIVITY_UNREADABLE_REASON, session_id, stamp
        )
        return float("inf")
    return when


def sweep_sessions(sessions: Any) -> list[str]:
    """Sweep against ``sessions`` (the session store); return the removed session ids.

    Nothing is swept when clio-core's metadata restore failed this boot (FN-14): the
    store then lacks sessions that still exist, and their folders would go as "gone".
    """

    from clio_agent.arc import clio_core_wal_repair  # noqa: PLC0415

    suspect = clio_core_wal_repair.restore_suspect()
    if suspect:
        logger.warning(
            "session scratch sweep skipped reason=%s cause=%s", RESTORE_SUSPECT_REASON, suspect
        )
        trace.event("SCRATCH", "%s cause=%s", RESTORE_SUSPECT_REASON, suspect)
        return []
    try:
        index = getattr(sessions, "index", None)
        indexed = frozenset(index.ids()) if index is not None else frozenset()
        return session_scratch.sweep(lambda sid: last_activity(sessions, sid, indexed))
    except (TranscriptNotReadyError, OSError) as exc:  # typed reason; retried next boot
        logger.warning("session scratch sweep stopped reason=%s error=%r", SWEEP_FAILED_REASON, exc)
        trace.event("SCRATCH", "%s error=%r", SWEEP_FAILED_REASON, exc)
        return []


async def sweep_when_readable(sessions: Any) -> list[str]:
    """Wait on the loop until the session store can be read, then sweep on a worker."""

    waited = 0.0
    while not getattr(sessions, "readable", True):
        if waited >= _READY_WAIT_S:
            trace.event("SCRATCH", "%s waited_s=%.0f", STORE_NOT_READY_REASON, waited)
            return []
        await asyncio.sleep(_READY_POLL_S)
        waited += _READY_POLL_S
    removed = await asyncio.to_thread(sweep_sessions, sessions)
    await asyncio.to_thread(sweep_short_roots)
    return removed


def sweep_short_roots() -> list[str]:
    """Remove stale short roots after the scratch folders they stood in for are gone."""

    from clio_agent.runtime import short_root_sweep  # noqa: PLC0415

    try:
        return short_root_sweep.sweep()
    except OSError as exc:  # typed reason; retried next boot
        logger.warning("short root sweep stopped reason=%s error=%r", SWEEP_FAILED_REASON, exc)
        trace.event("SCRATCH", "%s scope=short_roots error=%r", SWEEP_FAILED_REASON, exc)
        return []


def start(app: "FastAPI") -> None:
    """Run the sweep once (the real server's boot)."""

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return
    app.state.session_scratch_sweep = asyncio.get_running_loop().create_task(
        sweep_when_readable(sessions), name="clio-session-scratch-sweep"
    )


__all__ = [
    "ACTIVITY_UNREADABLE_REASON",
    "RECORD_UNREADABLE_REASON",
    "RESTORE_SUSPECT_REASON",
    "STORE_NOT_READY_REASON",
    "SWEEP_FAILED_REASON",
    "last_activity",
    "start",
    "sweep_short_roots",
    "sweep_when_readable",
]
