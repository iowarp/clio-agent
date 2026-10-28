"""SPOTTER quarantine: a danger alert stops the watched session until the person lifts it.

A ``danger`` finding from the session's armed SPOTTER watcher records an active
quarantine on the watched session (``metadata.spotter_quarantine``) and stops its
running turn (Conversation Stop: the watcher keeps running). While it is active the
session accepts no new messages and its mutating tool calls are refused with
:data:`QUARANTINE_REASON`. Only the person lifts it (``POST
/v1/sessions/{sid}/spotter/quarantine/lift``); the record stays, marked lifted.
A ``warning`` finding only posts its card.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from clio_agent.runtime import trace
from clio_agent.gact.events import Event
from clio_agent.gact.types import Session

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Session metadata key holding the quarantine record.
QUARANTINE_KEY = "spotter_quarantine"
#: Typed reason a quarantined session refuses a message or a mutating tool call.
QUARANTINE_REASON = "spotter_quarantined"
QUARANTINE_MESSAGE = (
    "SPOTTER put this session in quarantine: it detected a danger and stopped the session. "
    "The person reviews the finding and lifts the quarantine to continue."
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def active_quarantine(session: Any) -> dict[str, Any] | None:
    """The session's active quarantine record, or ``None``."""
    record = (getattr(session, "metadata", None) or {}).get(QUARANTINE_KEY)
    return record if isinstance(record, dict) and record.get("status") == "active" else None


def _publish(app: "FastAPI", sid: str, sess: Any, reason: str) -> None:
    payload = Session(**sess.to_wire()).model_dump(exclude_none=True)
    app.state.bus.publish(
        Event(type="session.updated", session_id=sid, payload={**payload, "reason": reason})
    )


def quarantine_session(
    app: "FastAPI",
    sid: str,
    *,
    title: str,
    detail: str,
    part_id: str,
    watcher_session_id: str,
    watcher_handle: str,
) -> bool:
    """Record an active quarantine on ``sid`` and stop its running turn.

    Returns ``False`` when the session is already quarantined (the first finding stays
    the reason; later danger cards still post).
    """
    session = app.state.sessions.get(sid)
    if session is None or active_quarantine(session) is not None:
        return False
    record = {
        "status": "active",
        "title": title,
        "detail": detail,
        "part_id": part_id,
        "watcher_session_id": watcher_session_id,
        "watcher_handle": watcher_handle,
        "raised_at": _now(),
    }
    sess = app.state.sessions.update(sid, metadata_patch={QUARANTINE_KEY: record})
    logger.warning("spotter_quarantine_started session=%s part=%s", sid, part_id)
    trace.event("SPOTTER", "spotter_quarantine_started session=%s part=%s", sid, part_id)
    if sess is not None:
        _publish(app, sid, sess, "spotter_quarantine_started")
    from clio_agent.gact.routes.session_cancellation import cancel_session_state  # noqa: PLC0415

    try:
        cancel_session_state(app, None, sid)
    except Exception as exc:  # noqa: BLE001 - the hold stands even if the stop fails
        logger.warning("spotter_quarantine_stop_failed session=%s error=%s", sid, exc)
    return True


def lift_quarantine(app: "FastAPI", sid: str) -> dict[str, Any] | None:
    """Lift ``sid``'s active quarantine (the person's decision); ``None`` if none is active."""
    session = app.state.sessions.get(sid)
    record = active_quarantine(session) if session is not None else None
    if record is None:
        return None
    lifted = {**record, "status": "lifted", "lifted_at": _now()}
    sess = app.state.sessions.update(sid, metadata_patch={QUARANTINE_KEY: lifted})
    logger.warning("spotter_quarantine_lifted session=%s", sid)
    trace.event("SPOTTER", "spotter_quarantine_lifted session=%s", sid)
    if sess is not None:
        _publish(app, sid, sess, "spotter_quarantine_lifted")
    return lifted


__all__ = [
    "QUARANTINE_KEY",
    "QUARANTINE_MESSAGE",
    "QUARANTINE_REASON",
    "active_quarantine",
    "lift_quarantine",
    "quarantine_session",
]
