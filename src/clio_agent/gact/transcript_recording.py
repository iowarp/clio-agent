"""Finite Core transcript writes and optional cache projection, with no execution policy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

from clio_agent.arc.lane_chunking import lane_segments
from clio_agent.arc.lane_generations import current_base, pointer_scope
from clio_agent.gact.messages import retained_message_position
from clio_agent.gact.metrics_counters import conversation_count, counts_toward_conversation
from clio_agent.gact.part_atoms import (
    MESSAGE_PART_SCOPE,
    append_part_atom,
    build_envelope_atom,
    build_sealed_part_atom,
    group_atoms_in_order,
    message_stub,
    reproduce_message_wire,
    retracted_part_ids,
)
from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)
Recording = Literal["recorded", "already_recorded", "withdrawn"]


class TranscriptRecordingPending(RuntimeError):
    """Retain exact completed output for recording retry, never model/finalize replay."""

    def __init__(self, message: Message, confirm: Callable[[], bool], cause: Exception) -> None:
        self.message = message.model_copy(deep=True)
        self.confirm = confirm
        self.after_confirmed: Callable[[], None] | None = None
        super().__init__(f"Transcript recording unconfirmed for {message.id}: {cause}")


def publication_step(
    completed: set[str], name: str, action: Callable[[], Any], *, retry: bool = True
) -> None:
    """Resume owner-local publication without replaying already completed steps.

    Effectful hooks and ephemeral events are attempted once. A raised hook may
    already have acted, so its error is reported rather than automatically replayed.
    Required projection writes may retry and must themselves be idempotent.
    This volatile progress is not a delivery acknowledgement or crash guarantee.
    """
    if name in completed:
        return
    try:
        action()
    except Exception:
        if retry:
            raise
        logger.exception("Transcript publication effect unconfirmed step=%s", name)
    completed.add(name)


def visibility_epoch(arc: Any, session_id: str) -> str:
    """Read the UI owner's mutation identity committed with an intentional lane replace."""
    store = arc._segments
    rows = store.list_segments(
        session_id, pointer_scope(MESSAGE_PART_SCOPE), include_tombstoned=False
    )
    return str(rows[0].content.get("transcript_visibility", "")) if rows else ""


def record_message(
    arc: Any,
    session_id: str,
    message: Message,
    *,
    expected_visibility: str | None = None,
) -> Recording:
    """Confirm one original message, recognizing uncertain prior writes before appending.

    Sealed streaming parts remain in place. The final envelope is written last.
    Intentional clear/rewind invalidates older volatile recordings; accepted-input
    splices preserve that identity. This function never reads a JSON projection.
    """
    if message.session_id != session_id:
        raise ValueError("Transcript message belongs to another session")
    store = arc._segments
    with store._lock_for(session_id, MESSAGE_PART_SCOPE):
        if (
            expected_visibility is not None
            and visibility_epoch(arc, session_id) != expected_visibility
        ):
            return "withdrawn"
        base = current_base(store, session_id, MESSAGE_PART_SCOPE)
        atoms = [
            row.content for row in lane_segments(store, session_id, base, include_tombstoned=False)
        ]
        retracted = retracted_part_ids(atoms)
        if any(part.id in retracted for part in message.parts if part.id):
            return "withdrawn"
        matches = []
        for group in group_atoms_in_order(atoms):
            first = group[0]
            header = first.get("message_stub") or first.get("message") or {}
            if first.get("message_id") == message.id and str(header.get("turn_id") or "") == str(
                message.turn_id or ""
            ):
                matches.append(group)
        if len(matches) > 1:
            raise ValueError("Completed transcript identity is ambiguous")
        current = matches[0] if matches else []
        complete = any(atom.get("atom_role") == "envelope" for atom in current)
        if complete:
            if Message.model_validate(reproduce_message_wire(current, retracted)) != message:
                raise ValueError("Completed transcript identity was reused with different output")
            return "already_recorded"
        if current and all(
            atom.get("envelope_authority", "inline") == "inline" for atom in current
        ):
            if Message.model_validate(reproduce_message_wire(current, retracted)) == message:
                return "already_recorded"
        stub = message_stub(
            message_id=message.id,
            turn_id=message.turn_id or "",
            session_id=session_id,
            created_at=message.created_at,
        )
        stub["role"] = message.role
        landed = {atom.get("part_id"): atom.get("part") for atom in current if atom.get("part")}
        for index, part in enumerate(message.parts):
            dump = part.model_dump()
            if part.id and landed.get(part.id) == dump:
                continue
            append_part_atom(
                store,
                session_id,
                build_sealed_part_atom(
                    stub,
                    dump,
                    index,
                    sealed_at=message.updated_at or message.created_at,
                    seal_source="finalize",
                ),
            )
        append_part_atom(store, session_id, build_envelope_atom(message))
        return "recorded"


def project_message(
    app: Any, session_id: str, message: Message, *, newly_recorded: bool, ordered: bool = False
) -> None:
    """Update rebuildable projections after Core confirms; cache errors are projection lag."""
    messages = app.state.messages
    resident = getattr(messages, "get_if_resident", None)
    rows = resident(session_id) if callable(resident) else messages.setdefault(session_id, [])
    existing = (
        []
        if rows is None
        else [
            index
            for index, row in enumerate(rows)
            if row.id == message.id and row.turn_id == message.turn_id
        ]
    )
    if rows is not None and len(existing) == 1:
        rows[existing[0]] = message
    elif rows is not None and not existing:
        position = (
            retained_message_position((row.created_at for row in rows), message.created_at)
            if ordered
            else len(rows)
        )
        rows.insert(position, message)
    counters = getattr(app.state, "metrics_counters", None)
    if newly_recorded and counters is not None:
        counters.add_message(session_id, message)
    sessions = getattr(app.state, "sessions", None)
    current = sessions.get(session_id) if sessions is not None else None
    count: int | None = None
    if current is not None and newly_recorded:
        count = current.message_count + (1 if counts_toward_conversation(message) else 0)
    elif current is not None:
        # A lost write reply can leave an envelope confirmed but its count
        # unprojected. Recount from Core only on explicit recording recovery.
        from clio_agent.gact.transcript_projection import assemble_session_messages

        count = conversation_count(assemble_session_messages(app.state.arc, session_id))
    if sessions is not None and current is not None and count is not None:
        if current.message_count != count:
            try:
                sessions.update(session_id, message_count=count)
            except (OSError, RuntimeError, ValueError):
                logger.exception(
                    "Session count cache lag session=%s; canonical Core output is retained",
                    session_id,
                )
    cache = getattr(app.state, "message_store", None)
    if cache is not None:
        try:
            cache.ensure_message(session_id, message)
        except (OSError, RuntimeError, ValueError):
            logger.exception(
                "Transcript cache lag session=%s message=%s; canonical Core output is retained",
                session_id,
                message.id,
            )


async def resume_recording(app: Any, pending: TranscriptRecordingPending) -> bool:
    """Await owner-local recording/publication custody; Stop may cancel only this waiter."""

    owner = getattr(app.state, "conversations", None)
    if owner is None:
        raise pending
    return await owner.confirm_transcript_recording(pending)
