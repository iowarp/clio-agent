"""Turns a restart interrupted: marked at startup from the records, closed per conversation.

A user message is durable before its assistant turn starts, so a process that stops
mid-turn leaves the conversation recorded ``running`` with no executor, and a
transcript that ends in the user's message. Two things follow, at different costs:

* **The mark** -- the record says ``error`` with ``metadata.restart_interruption``. It
  is read from the session records alone, so startup does it for every such
  conversation before ``ready`` (:func:`mark_restart_interrupted`); every request sees
  the interrupted state from the first moment.
* **The transcript** -- the typed interrupted-turn row after the dangling user message
  and the record's message count. That reads the conversation's history, so it is
  per-conversation work done after ``ready``, by whichever comes first:

  - the obligations lane, oldest first (:meth:`RestartInterruptions.settle_all`);
  - opening the conversation: the first materialization of its ledger
    (:func:`settle_restart_interruption`, wired as the resident set's rehydrate hook);
  - any transcript write to it, which closes the interrupted turn before the write
    lands, so nothing is ever appended after a dangling user message.

  A turn whose own sealed parts are the transcript's last row (an incomplete
  assistant message) keeps them, and gets the same typed reason. Either closing row
  also gets each call of the turn that started with no recorded outcome, typed
  ``tool_outcome_unrecorded`` (:mod:`clio_agent.gact.interrupted_tool_calls`), so it
  reads as unknown.

The mark carries ``transcript: "pending"`` until the transcript is closed, so a stop
before that resumes it on the next start. Nothing here is a new store: the pending set
is the session records' own mark, held in memory for the cheap per-write check.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, Optional

from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)

RESTART_INTERRUPTED_REASON = "server_restart_interrupted"
#: Typed reason when closing an interrupted transcript failed (it stays pending).
SETTLE_FAILED_REASON = "restart_interruption_settle_failed"
TRANSCRIPT_PENDING = "pending"

#: Metadata on a reply the restart's recovery produced: the turn id it continues. A
#: client shows that interrupted turn as resumed, without Retry (CC-O1).
RESUMES_TURN = "resumes_turn"


def resumed_turn_metadata(metadata: object) -> dict[str, str]:
    """The ``resumes_turn`` entry a recovery turn's input carries, for its reply."""

    value = metadata.get(RESUMES_TURN) if isinstance(metadata, dict) else None
    return {RESUMES_TURN: value} if isinstance(value, str) and value else {}


def note_resumed_turn(history: object, items: Iterable[object], turn_id: str) -> None:
    """Remember that ``turn_id`` took in a recovery that continues an interrupted turn.

    The recovery input is not always the turn's trigger (a finished task's notice can
    start the turn and the recovery arrive with it), so the turn that commits it is
    the one that resumed the work. Its reply's metadata gets ``resumes_turn`` at
    finalize (:func:`resumed_by_turn`), like the summons outcome, held per app.
    """
    from clio_agent.gact.runtime.app_state import per_app_dict  # noqa: PLC0415

    for item in items:
        if getattr(item, "kind", "") != "control":
            continue
        found = resumed_turn_metadata(history.read_content(item.content))  # type: ignore[attr-defined]
        if found:
            per_app_dict("resumed_turns")[turn_id] = found[RESUMES_TURN]


def resumed_by_turn(turn_id: str) -> dict[str, str]:
    """The ``resumes_turn`` metadata ``turn_id`` committed, once (finalize reads it)."""
    from clio_agent.gact.runtime.app_state import per_app_dict  # noqa: PLC0415

    value = per_app_dict("resumed_turns").pop(turn_id, None)
    return {RESUMES_TURN: value} if value else {}


__all__ = [
    "RESUMES_TURN",
    "resumed_turn_metadata",
    "note_resumed_turn",
    "resumed_by_turn",
    "RESTART_INTERRUPTED_REASON",
    "SETTLE_FAILED_REASON",
    "RestartInterruptions",
    "mark_restart_interrupted",
    "settle_restart_interruption",
]


def _mark(*, pending: bool) -> dict[str, dict[str, str]]:
    info = {"reason": RESTART_INTERRUPTED_REASON, "previous_status": "running"}
    if pending:
        info["transcript"] = TRANSCRIPT_PENDING
    return {"restart_interruption": info}


def mark_restart_interrupted(app: "FastAPI") -> None:
    """Startup: mark every conversation still recorded ``running`` as interrupted.

    Reads only the records the status index names (``running``, and ``error`` for the
    conversations an earlier start marked but never closed) -- no transcript. Installs
    the owner on ``app.state.restart_interruptions`` (oldest first).
    """

    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415
    from clio_agent.gact.transcript_file import has_durable_transcript_store  # noqa: PLC0415

    sessions = getattr(app.state, "sessions", None)
    if sessions is None or not has_durable_transcript_store(app):
        return
    query = PageQuery(archived=None, statuses=frozenset({"running", "error"}))
    pending: list[str] = []
    for session in reversed(sessions.select(query)):  # newest created first -> oldest first
        info = session.metadata.get("restart_interruption")
        if session.status == "running":
            sessions.update(session.id, status="error", metadata_patch=_mark(pending=True))
            trace.event("SESSION", "restart_interrupted session=%s transcript=pending", session.id)
        elif not (isinstance(info, dict) and info.get("transcript") == TRANSCRIPT_PENDING):
            continue
        pending.append(session.id)
    app.state.restart_interruptions = RestartInterruptions(app, pending)


def settle_restart_interruption(
    app: "FastAPI", session_id: str, messages: Optional[list["Message"]] = None
) -> bool:
    """Close ``session_id``'s interrupted transcript if it is still pending; cheap if not."""

    owner = getattr(app.state, "restart_interruptions", None)
    if owner is None:
        return False
    return owner.settle(session_id, messages)


class RestartInterruptions:
    """The conversations whose interrupted transcript is not closed yet, oldest first."""

    def __init__(self, app: "FastAPI", pending: list[str]) -> None:
        self.app = app
        self._locks: dict[str, threading.RLock] = {sid: threading.RLock() for sid in pending}
        self._settling: set[str] = set()
        self.settled = 0

    def pending(self) -> tuple[str, ...]:
        """Conversations still waiting, oldest first."""
        return tuple(self._locks.copy())  # closed from other threads meanwhile

    def waiting(self, session_id: str) -> bool:
        """Whether ``session_id``'s interrupted transcript is not closed yet."""
        return session_id in self._locks

    def settle(self, session_id: str, messages: Optional[list["Message"]] = None) -> bool:
        """Close one interrupted transcript; ``True`` when this call closed it.

        Concurrent callers for the same conversation wait for the first one; a re-entrant
        call from the closing write itself returns at once. ``messages`` is the durable
        transcript when the caller already read it (the rehydrate hook).
        """

        lock = self._locks.get(session_id)
        if lock is None:
            return False
        with lock:
            if session_id in self._settling or session_id not in self._locks:
                return False
            self._settling.add(session_id)
            try:
                closed = _close_transcript(self.app, session_id, messages)
            finally:
                self._settling.discard(session_id)
            if closed:
                self._locks.pop(session_id, None)
                self.settled += 1
            return closed

    def settle_quietly(self, session_id: str) -> bool:
        """Settle from the obligations lane without leaving its ledger resident."""

        messages = self.app.state.messages
        was_resident = messages.get_if_resident(session_id) is not None
        closed = self.settle(session_id)
        if closed and not was_resident:
            discard = getattr(messages, "discard", None)
            if callable(discard):
                discard(session_id)  # the next open re-reads the record, as for any session
        return closed

    def snapshot(self) -> dict[str, int]:
        """Progress for diagnostics."""
        return {"pending": len(self._locks), "settled": self.settled}


def _close_transcript(app: "FastAPI", session_id: str, messages: Optional[list["Message"]]) -> bool:
    """Append the interrupted-turn row after a dangling user message; settle the record."""

    from clio_agent.gact.session_store import (  # noqa: PLC0415 - import cycle
        _interrupted_assistant_row,
        _replace_session_messages,
        restart_error_info,
    )

    sessions = app.state.sessions
    try:
        if messages is None:
            messages = _read_transcript(app, session_id)
        durable = list(messages or [])
        last = durable[-1] if durable else None
        closing = None
        if last is not None and last.role == "user":
            closing = _interrupted_assistant_row(app, session_id, last)
            durable.append(closing)
        elif last is not None and last.role == "assistant" and last.stop_reason == "incomplete":
            # The turn's own sealed parts are the last row: it keeps them as they are,
            # plus any call whose outcome was never recorded (interrupted_tool_calls),
            # and says why it ended, as the row after a dangling user message does. A
            # turn cut mid answer otherwise read as a reply that stopped for no reason.
            closing = _with_unrecorded_calls(app, session_id, last).model_copy(
                update={
                    "stop_reason": "error",
                    "error_info": restart_error_info(session_id, last.turn_id or last.id),
                }
            )
            durable[-1] = closing
        if closing is not None and closing is not last:
            # Through the sanctioned replace seam (undo/rewind use it too), so the
            # atom lane and the resident ledger agree by construction (#1334 F2).
            _replace_session_messages(app, session_id, durable)
    except OSError as exc:
        logger.error(
            "restart interruption settle failed reason=%s session=%s error=%r",
            SETTLE_FAILED_REASON,
            session_id,
            exc,
        )
        trace.event(
            "SESSION", "restart_interrupted session=%s reason=%s", session_id, SETTLE_FAILED_REASON
        )
        return False
    sessions.update(session_id, message_count=len(durable), metadata_patch=_mark(pending=False))
    trace.event(
        "SESSION",
        "restart_interrupted session=%s messages=%s transcript=closed",
        session_id,
        len(durable),
    )
    return True


def _with_unrecorded_calls(app: "FastAPI", session_id: str, row: "Message") -> "Message":
    from clio_agent.gact.interrupted_tool_calls import with_unrecorded_calls  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import canonical_log_arc  # noqa: PLC0415

    arc = canonical_log_arc(app)
    if arc is None:
        raise OSError("restart interruption settle needs the canonical Core store")
    return with_unrecorded_calls(arc, session_id, row.turn_id or row.id, row)


def _read_transcript(app: "FastAPI", session_id: str) -> list["Message"]:
    """The conversation's recorded transcript, read under its lane lock only.

    Deliberately not :func:`~clio_agent.gact.transcript_file.materialize_from_atoms`:
    that also takes the task history's lock to project delivery display, and a writer
    holding that lock may be waiting for this settle (lock order: history, then here).
    """

    from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - import cycle
        assemble_session_messages,
        canonical_log_arc,
        has_atoms,
    )

    arc = canonical_log_arc(app)
    if arc is None:
        raise OSError("restart interruption settle needs the canonical Core store")
    with arc._segments._lock_for(session_id, MESSAGE_PART_SCOPE):
        if not has_atoms(arc, session_id):
            return []
        return assemble_session_messages(arc, session_id)
