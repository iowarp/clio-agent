"""Identify and restore the same session's existing context plane."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any


def context_identity(arc: Any, session: str, scope: str) -> dict[str, Any]:
    """Describe the actual plane without reconstructing or copying its history."""
    store = getattr(arc, "_store", None)
    return {
        "process_id": os.getpid(),
        "mode": "clio_core" if store is not None else "history",
        "session_id": session,
        "scope": scope,
        "has_segments": bool(arc and arc.has_segments(session, scope)),
        "store_config": str(getattr(store, "_config_path", "")),
        "store_namespace": str(getattr(store, "_namespace", "")),
    }


def restore_context(
    arc: Any,
    session: str,
    scope: str,
    turn_id: str,
    ledger: Sequence[Any],
    *,
    allow_transcript: bool = True,
) -> str:
    """Restore only an absent exact scope; prepared input never becomes a receipt.

    No predecessor history is copied. A known empty epoch or tombstoned scope is
    intentional. Missing trace/proof and failed reads are distinct: errors propagate.
    The note (the ``context_recovery_*`` reminders) is ``""`` when switched off; the
    context is restored either way.
    """
    if arc.has_segments(session, scope):
        return ""
    import uuid

    from clio_agent.arc.context_restore import recorded_segments, unanswered_calls
    from clio_agent.arc.schema import Segment
    from clio_agent.gact import context, reminders
    from clio_agent.gact.agents.clio_react_record import fold_steps
    from clio_agent.gact.agents.context_reader import model_context_segments
    from clio_agent.gact.conversation_projection import _get, transcript_segments

    state = getattr(context.active_app(), "state", None)
    history = getattr(state, "task_history", None)
    reader = getattr(getattr(state, "semantic_trace_backend", None), "session_events", None)
    # Capture canonical proof before any restoration; indexes and trace intents
    # cannot authenticate their own input record on the next replay.
    confirmed = history.context_inputs(session, scope) if history is not None else {}
    retained_turns = {
        str(identity)
        for row in ledger
        for identity in (_get(row, "turn_id"), _get(row, "id"))
        if identity
    }
    restored, unconfirmed, found = recorded_segments(
        reader(session) if callable(reader) else (), session, scope, retained_turns, confirmed
    )
    origin = "operations"
    if not found:
        if not allow_transcript:
            return ""
        if "#epoch:" in scope:
            boundary = context.current_turn().context_epoch_after
            if boundary is None:
                return ""  # no proof that transcript rows belong to this fresh epoch
            ledger = [
                row
                for row in ledger
                if context.after_context_boundary(_get(row, "created_at"), boundary) is True
            ]
        rows = [
            row
            for row in ledger
            if not (turn_id and turn_id in {_get(row, "id"), _get(row, "turn_id")})
        ]
        if not turn_id and rows and _get(rows[-1], "role") == "user":
            rows.pop()
        restored = transcript_segments(session, scope, rows)
        origin = "transcript"
    live = [segment for segment in restored if segment.status == "live"]
    recovered: list[Segment] = []
    pending: dict[str, Segment] = {}
    clock = max((segment.logical_time for segment in restored), default=0)
    uncertain = reminders.render("context_recovery_uncertain", {}, session_id=session) or ""

    def close_step(boundary: float | None) -> None:
        nonlocal clock
        start = recovered[-1].order if recovered else 0.0
        stop = boundary if boundary is not None else start + len(pending) + 1
        previous = start
        for position, call in enumerate(pending.values(), 1):
            order = start + (stop - start) * position / (len(pending) + 1)
            if not previous < order < stop:
                raise ValueError("No representable context order for uncertain tool evidence")
            clock += 1
            recovered.append(
                Segment(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, session + "\0" + scope + "\0" + call.id).hex,
                    session_id=session,
                    scope=scope,
                    kind="observation",
                    step=call.step,
                    turn_id=call.turn_id,
                    order=order,
                    logical_time=clock,
                    content={
                        "call_id": call.content["id"],
                        "is_error": True,
                        "text": uncertain,
                    },
                )
            )
            previous = order
        pending.clear()

    positions: dict[int, list[Segment]] = {}
    for position, call in unanswered_calls(restored):
        positions.setdefault(position, []).append(call)
    for position in range(len(restored) + 1):
        pending.update({call.content["id"]: call for call in positions.get(position, ())})
        if pending:
            close_step(restored[position].order if position < len(restored) else None)
        if position < len(restored):
            recovered.append(restored[position])
    restored = recovered
    if not live and not unconfirmed:
        if restored:
            arc.restore_empty_context(session, scope, restored)
        return ""
    pieces = [
        f"context_recovery_{origin}",
        *(["context_recovery_unconfirmed"] if unconfirmed else []),
    ]
    note = " ".join(
        text for piece in pieces if (text := reminders.render(piece, {}, session_id=session))
    )
    if note:
        notice = Segment(
            session_id=session,
            scope=scope,
            kind="user",
            step=0,
            turn_id=turn_id,
            order=max((segment.order for segment in restored), default=0) + 1,
            logical_time=max((segment.logical_time for segment in restored), default=0) + 1,
            content={"text": note, "actor": "algorithm", "source": "context_recovery"},
        )
        restored.append(notice)
    fold_steps(
        model_context_segments(
            [segment for segment in restored if segment.status == "live"], history, session, scope
        )
    )
    return note if arc.restore_empty_context(session, scope, restored) else ""
