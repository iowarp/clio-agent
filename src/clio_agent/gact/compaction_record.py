"""Writing a compaction's record where it happened, durably, BEFORE its fold.

The record is the summarization injection (:mod:`clio_agent.gact.summarization_record`):

* **mid-turn** (a turn's transcript is open): a part of the open turn's assistant
  message, at the step boundary it happened at;
* **between turns**: its own assistant row holding that one part.

:func:`write_record` makes the record durable on clio-core first (the part atom, plus
the row's envelope between turns) and returns a :class:`PendingRecord`. The caller then
folds: on success :meth:`PendingRecord.publish` shows it (the open transcript, or the
ledger and ``message.created``); on a failed fold :meth:`PendingRecord.retract` writes a
retract atom, so the record is never served. A record that cannot be written raises
:class:`RecordWriteError` and nothing is folded. There is no staging.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clio_agent.errors import ClioError
from clio_agent.gact.parts import Part

logger = logging.getLogger(__name__)

__all__ = ["PendingRecord", "RecordWriteError", "write_notice", "write_record"]

#: Typed reason when a mid-turn record lands after its turn settled.
RECORD_AFTER_SETTLE = "compaction_record_after_settle"


class RecordWriteError(ClioError):
    """A compaction's record could not be written durably; nothing was folded."""

    reason = "compaction_record_write_failed"

    def __init__(self, session_id: str, stage: str, cause: BaseException | str) -> None:
        super().__init__(
            f"the compaction record of session {session_id} could not be written "
            f"({stage}): {cause}",
            error_type=self.reason,
            details={"session_id": session_id, "stage": stage},
        )


@dataclass
class PendingRecord:
    """A durable record, not yet shown.

    Attributes:
        message_id: The assistant message holding the record part.
        part_id: The record part's id.
        turn_id: The open turn's id, or ``""`` between turns.
        publish: Show the record (call once, after the fold succeeded).
        retract: Make the durable record unservable (call once, after a failed fold).
    """

    message_id: str
    part_id: str
    turn_id: str
    publish: Callable[[], None]
    retract: Callable[[], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_record(app: Any, sid: str, part: Part) -> PendingRecord:
    """Write ``part`` durably where the compaction happened. Blocking, off-loop only.

    Raises:
        RecordWriteError: the record could not be made durable (nothing written stays
            served); the caller must not fold.
    """
    from clio_agent.gact.tool_observer import _session_turn_transcript  # noqa: PLC0415

    transcript = _session_turn_transcript(app, sid)
    if transcript is not None and not transcript.frozen:
        return _in_open_turn(app, sid, transcript, part)
    return _own_row(app, sid, part)


def write_notice(app: Any, sid: str, part: Part) -> tuple[str, str]:
    """Record a failed compaction's ``notice`` where it happened; ``(message, part)`` ids.

    Mid-turn it joins the open turn's assistant message (persisted with it, also when
    the turn then fails); between turns it is its own row, minted now. Raises whatever
    the write raises (the caller reports both failures typed).
    """
    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.session_store import _append_session_message  # noqa: PLC0415
    from clio_agent.gact.tool_observer import (  # noqa: PLC0415
        _mirror_transcript_state,
        _session_turn_transcript,
    )
    from clio_agent.gact.types import Message, Tokens  # noqa: PLC0415

    transcript = _session_turn_transcript(app, sid)
    if transcript is not None and transcript.append_part(part) is not None:
        _mirror_transcript_state(app, sid, transcript)
        return str(transcript.message_id), str(part.id)
    now = _now_iso()
    part.sequence = 1
    message = Message(
        id=f"msg_notice_{uuid.uuid4().hex[:12]}",
        turn_id="",
        session_id=sid,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=[part],
        tokens=Tokens(),
        stop_reason="end_turn",
        metadata={"synthetic": "compaction_failed", "compaction_id": part.compaction_id},
    )
    _append_session_message(app, sid, message)
    app.state.sessions.update(sid, message_count=len(app.state.messages.get(sid, [])))
    app.state.bus.publish(Event(type="message.created", session_id=sid, payload=message.to_wire()))
    return message.id, str(part.id)


def _in_open_turn(app: Any, sid: str, transcript: Any, part: Part) -> PendingRecord:
    from clio_agent.gact.part_atom_minter import turn_minter  # noqa: PLC0415
    from clio_agent.gact.part_atoms import build_retract_atom, message_stub  # noqa: PLC0415
    from clio_agent.gact.tool_observer import _mirror_transcript_state  # noqa: PLC0415

    minter = turn_minter(app, sid)
    if minter is None or minter.arc is None:
        raise RecordWriteError(sid, "open_turn", "the turn has no clio-core transcript writer")
    try:
        message_id = transcript.ensure_message()
        index = len(transcript.snapshot())
        part.sequence = index + 1
        minter.seal_now(message_id, part.model_dump(), index, source="compaction")
    except Exception as exc:  # noqa: BLE001 - raised typed: the caller folds nothing
        raise RecordWriteError(sid, "open_turn", exc) from exc

    def publish() -> None:
        if transcript.append_part(part) is None:
            # The turn settled between the write and the fold: the durable atom is the
            # record (reload shows it); the live view could not take it.
            logger.warning(
                "compaction record not shown live reason=%s session=%s part=%s",
                RECORD_AFTER_SETTLE,
                sid,
                part.id,
            )
            return
        _mirror_transcript_state(app, sid, transcript)

    def retract() -> None:
        from clio_agent.gact.part_atoms import append_part_atom  # noqa: PLC0415

        stub = message_stub(
            message_id=message_id,
            turn_id=minter.turn_id,
            session_id=sid,
            created_at=minter.opened_at,
        )
        atom = build_retract_atom(stub, [str(part.id)], index)
        minter.run_now(
            f"retract:{part.id}", lambda: append_part_atom(minter.arc._segments, sid, atom)
        )

    return PendingRecord(message_id, str(part.id), transcript.turn_id, publish, retract)


def _own_row(app: Any, sid: str, part: Part) -> PendingRecord:
    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.part_atoms import (  # noqa: PLC0415
        append_part_atom,
        build_envelope_atom,
        build_retract_atom,
        build_sealed_part_atom,
        message_stub,
    )
    from clio_agent.gact.session_store import _append_session_message  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import canonical_log_arc  # noqa: PLC0415
    from clio_agent.gact.types import Message, Tokens  # noqa: PLC0415

    arc = canonical_log_arc(app)
    if arc is None:
        raise RecordWriteError(sid, "own_row", "no clio-core transcript lane")
    now = _now_iso()
    part.sequence = 1
    message = Message(
        id=f"msg_summary_{uuid.uuid4().hex[:12]}",
        turn_id="",
        session_id=sid,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=[part],
        tokens=Tokens(),
        stop_reason="end_turn",
        metadata={"synthetic": "summarization", "compaction_id": part.compaction_id},
    )
    stub = message_stub(message_id=message.id, turn_id="", session_id=sid, created_at=now)
    store = arc._segments
    written: list[str] = []
    try:
        append_part_atom(
            store,
            sid,
            build_sealed_part_atom(
                stub, part.model_dump(), 0, sealed_at=now, seal_source="compaction"
            ),
        )
        written.append(str(part.id))
        append_part_atom(store, sid, build_envelope_atom(message))
    except Exception as exc:  # noqa: BLE001 - raised typed after the landed part is retracted
        if written:
            append_part_atom(store, sid, build_retract_atom(stub, written, 0))
        raise RecordWriteError(sid, "own_row", exc) from exc

    def publish() -> None:
        _append_session_message(app, sid, message, atoms_minted=True)
        app.state.sessions.update(sid, message_count=len(app.state.messages.get(sid, [])))
        app.state.bus.publish(
            Event(type="message.created", session_id=sid, payload=message.to_wire())
        )

    def retract() -> None:
        append_part_atom(store, sid, build_retract_atom(stub, [str(part.id)], 0))

    return PendingRecord(message.id, str(part.id), "", publish, retract)
