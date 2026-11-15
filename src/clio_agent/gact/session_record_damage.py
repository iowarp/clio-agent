"""A session record that no longer reads back is reported, never treated as deleted (FN-14).

After a torn clio-core metadata record, clio-core's allocation log and its metadata can
disagree about a blob's blocks, and a later write can land on another blob's block: live,
a conversation's record read back as ``1``. The session index still lists it, but its
record does not decode, so every route answered ``404 session not found`` and the boot
scratch sweep removed its folder as gone.

:func:`note_unreadable` records such a session (logged, audited with
:data:`~clio_agent.gact.session_records.ROW_UNREADABLE_REASON`); :func:`missing_session`
then answers ``503 session_record_unreadable`` with a plain message instead of 404, and
the ``clio_core_metadata_restore`` health row lists the sessions. Nothing is deleted:
the scratch sweep keeps the folder of every session the index lists.
"""

from __future__ import annotations

import logging
import threading

from fastapi import HTTPException

from clio_agent.gact.session_records import ROW_UNREADABLE_REASON
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_unreadable: dict[str, str] = {}


def note_unreadable(session_id: str, error: BaseException) -> None:
    """Record that ``session_id``'s stored record does not decode (loud, once per id)."""
    stream_audit(
        ROW_UNREADABLE_REASON,
        reason=ROW_UNREADABLE_REASON,
        session_id=session_id,
        error=type(error).__name__,
    )
    with _lock:
        first = session_id not in _unreadable
        _unreadable[session_id] = type(error).__name__
    if first:
        logger.error(
            "session record unreadable reason=%s session=%s error=%s: the index lists it but "
            "its record does not read back; it is kept, not deleted",
            ROW_UNREADABLE_REASON,
            session_id,
            type(error).__name__,
        )


def unreadable_sessions() -> dict[str, str]:
    """``{session id: decode error}`` of the records this process could not read."""
    with _lock:
        return dict(_unreadable)


def missing_session(session_id: str, not_found: HTTPException) -> HTTPException:
    """``not_found`` for a session that does not exist; a typed 503 for a damaged one."""
    with _lock:
        damaged = session_id in _unreadable
    if not damaged:
        return not_found
    return HTTPException(
        status_code=503,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=ROW_UNREADABLE_REASON,
                message=(
                    "This conversation's saved record could not be read back (the store "
                    "returned other data for it), so it cannot open. It has not been "
                    "deleted; its folder and transcript are kept."
                ),
                details={"session_id": session_id, "reason": ROW_UNREADABLE_REASON},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def reset_for_tests() -> None:
    """Forget the recorded sessions (tests)."""
    with _lock:
        _unreadable.clear()


__all__ = ["missing_session", "note_unreadable", "reset_for_tests", "unreadable_sessions"]
