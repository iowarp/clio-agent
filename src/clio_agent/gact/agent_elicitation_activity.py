"""Durable activity projection helpers for agent-addressed MCP elicitation."""

from __future__ import annotations

from typing import Any


def stamp_agent_answer_turn(app: Any, question_id: str, handle: Any) -> None:
    """Attach an answer helper's stable task identity to its causal question."""

    from clio_agent.gact.elicitation_bridge import (  # noqa: PLC0415
        stamp_question_routing_fields,
    )
    from clio_agent.gact.events import Event  # noqa: PLC0415

    current = getattr(app.state, "user_questions", {}).get(question_id)
    if current is None:
        return
    metadata = dict(current.metadata)
    metadata["agent_answer_task"] = {
        "task_id": str(getattr(handle, "task_id", "") or ""),
        "child_session_id": str(getattr(handle, "child_session_id", "") or ""),
    }
    updated = stamp_question_routing_fields(app, question_id, metadata=metadata)
    bus = getattr(app.state, "bus", None)
    if updated is not None and bus is not None:
        bus.publish(
            Event(
                type="user_question.updated",
                session_id=updated.session_id,
                payload=updated.model_dump(exclude_none=True),
            )
        )
