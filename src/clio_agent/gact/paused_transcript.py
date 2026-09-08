"""Persist observed assistant activity at an input-required turn boundary."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from clio_agent.gact.events import Event, _publish_transcript_event
from clio_agent.gact.runtime.globals import _emit_semantic_event
from clio_agent.gact.transcript_projection import final_message_embed
from clio_agent.gact.types import Message, Tokens

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState


def persist_paused_transcript(state: "TurnState") -> str:
    """Save existing parts verbatim, without fabricating an answer or completing work.

    Input-required turns bypass normal finalization. Close and persist their
    existing ledger before it is retired so reload has the same tool activity.
    The question/approval ledger remains authoritative for the pending input.
    """
    from clio_agent.gact.app import _append_session_message  # noqa: PLC0415

    if not state.transcript.snapshot():
        return ""
    message_id = state.transcript.ensure_message()
    if any(m.id == message_id for m in state.app.state.messages.get(state.sid, [])):
        return message_id
    now = datetime.now(timezone.utc).isoformat()
    message = Message(
        id=message_id,
        turn_id=state.turn_id,
        session_id=state.sid,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=state.transcript.finalize(),
        tokens=Tokens(**state.turn_tokens),
        cost_usd=state.turn_cost,
        stop_reason="waiting_user",
        metadata=state.assistant_metadata,
    )
    _append_session_message(state.app, state.sid, message)
    payload = {
        "turn_id": state.turn_id,
        "message_id": message.id,
        "stop_reason": "waiting_user",
        "tokens": dict(state.turn_tokens),
        "cost_usd": state.turn_cost,
    }
    _emit_semantic_event(
        state.app,
        state.sid,
        "turn.completed",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="waiting_user",
        summary="Turn paused for user input.",
        actor={"agent_id": state.selected_agent or state.invocation_agent_id},
        subject={"message_id": message.id},
        payload={**payload, **final_message_embed(state.app, state.sid, message)},
    )
    _publish_transcript_event(state.bus, state.sid, "turn.completed", {"turn_id": state.turn_id})
    state.bus.publish(Event(type="message.completed", session_id=state.sid, payload=payload))
    # The paused turn is now durable. A later answer starts a distinct turn;
    # do not adopt and re-append this already persisted message as an in-flight one.
    for name in ("live_assistant_message_ids", "live_assistant_parts", "live_assistant_part_keys"):
        getattr(state.app.state, name, {}).pop(state.sid, None)
    return message.id
