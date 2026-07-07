"""Nanoagent (Tier 3 subagent) spawn materialisation for the GACT turn engine (#767 Phase B).

Slice 2 of the ``turn.py`` decomposition: the nanoagent spawn loop that used to
live inline in ``finalize_turn`` moves here as a free function taking
:class:`~clio_agent.gact.turn_state.TurnState` first (the gact seam convention).

The loop is behavior-preserving. For each spawn recorded on the prediction it
creates a child (subagent) session, materialises its user + assistant messages,
and publishes the ``subagent.started`` / ``subagent.completed`` semantic events
and bus events — exactly as the former linear body did. It mutates
``state.tools_called`` per spawn, mirroring today's body-level reassignment.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from clio_agent.gact.events import Event
from clio_agent.gact.messaging import _format_subagent_input
from clio_agent.gact.runtime.globals import (
    _emit_semantic_event,
    _iso_from_epoch,
    _new_message_id,
    _new_part_id,
)
from clio_agent.gact.session_store import _extend_session_messages
from clio_agent.gact.types import Message, Part

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState


def spawn_nanoagents(
    state: "TurnState",
    nanoagents: list[Any],
    assistant_msg: Message,
    sess: Any,
) -> None:
    """Materialise nanoagent spawns + publish their lifecycle events (#767 Phase B).

    For each spawn on the turn's prediction, creates a child subagent session,
    writes its user + assistant messages, and publishes the paired
    ``subagent.started`` / ``subagent.completed`` semantic + bus events. Mutates
    ``state.tools_called`` per spawn, exactly as the former linear body did.

    Args:
        state: The active turn's mutable working set.
        nanoagents: The spawn records harvested from the prediction.
        assistant_msg: The parent turn's finalized assistant message.
        sess: The parent session (owns the workspace the children inherit).
    """

    for spawn in nanoagents:
        get = (
            spawn.get
            if isinstance(spawn, dict)
            else (lambda k, default=None, _s=spawn: getattr(_s, k, default))
        )
        agent_id = get("agent_id") or get("agent") or "nanoagent"
        spawn_input = get("input") or {}
        answer = get("answer") or ""
        state.tools_called = get("tools_called") or get("tools") or []
        subsess = state.app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=f"{agent_id} subagent",
            parent_session_id=state.sid,
            agent={"id": str(agent_id), "mode": "subagent"},
            metadata={
                "session_type": "nanoagent",
                "agent_id": str(agent_id),
                "parent_session_id": state.sid,
                "spawned_by_message_id": assistant_msg.id,
                "spawned_by_agent": state.selected_agent,
                "tool_count": len(state.tools_called) if isinstance(state.tools_called, list) else 0,
            },
        )
        sub_now = time.time()
        sub_user = Message(
            id=_new_message_id("user"),
            session_id=subsess.id,
            role="user",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[
                Part(
                    id=_new_part_id(),
                    type="text",
                    text=_format_subagent_input(spawn_input),
                )
            ],
            metadata={
                "subagent_input": spawn_input,
                "parent_session_id": state.sid,
                "spawned_by_message_id": assistant_msg.id,
            },
        )
        sub_asst = Message(
            id=_new_message_id("asst"),
            session_id=subsess.id,
            role="assistant",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=(
                [Part(id=_new_part_id(), type="text", agent_id=str(agent_id), text=answer)]
                if answer
                else []
            ),
            stop_reason="end_turn",
            metadata={"tools_called": state.tools_called} if state.tools_called else {},
        )
        _extend_session_messages(state.app, subsess.id, [sub_user, sub_asst])
        state.app.state.sessions.update(subsess.id, message_count=2, status="idle")
        _emit_semantic_event(
            state.app,
            state.sid,
            "subagent.started",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="running",
            summary=f"Spawned subagent {agent_id}.",
            actor={"agent_id": state.selected_agent or "orchestrator"},
            subject={"agent_id": str(agent_id), "session_id": subsess.id},
            payload={
                "parent_session_id": state.sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "spawned_by_message_id": assistant_msg.id,
            },
        )
        state.bus.publish(
            Event(
                type="subagent.started",
                session_id=state.sid,
                payload={
                    "parent_session_id": state.sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "spawned_by_message_id": assistant_msg.id,
                },
            )
        )
        _emit_semantic_event(
            state.app,
            state.sid,
            "subagent.completed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            summary=f"Subagent {agent_id} completed.",
            actor={"agent_id": str(agent_id), "session_id": subsess.id},
            subject={"session_id": state.sid},
            payload={
                "parent_session_id": state.sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                "tokens": get("tokens") or {},
                "cost_usd": float(get("cost_usd", 0.0) or 0.0),
            },
        )
        state.bus.publish(
            Event(
                type="subagent.completed",
                session_id=state.sid,
                payload={
                    "parent_session_id": state.sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                    "tokens": get("tokens") or {},
                    "cost_usd": float(get("cost_usd", 0.0) or 0.0),
                },
            )
        )
