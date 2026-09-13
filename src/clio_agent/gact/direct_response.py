"""Structural transcript transition for a tool-free ReAct completion."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from clio_agent.gact.transcript import TurnTranscript


def promote_tool_free_response(
    transcript: TurnTranscript,
    prediction: Any,
    agent_ids: Iterable[str],
) -> bool:
    """Promote the current ``next_thought`` part to the answer channel.

    ReAct exposes prose before the provider's tool-call list is known. An empty
    list makes that same part the terminal response. The transition uses the
    current producer identity only: it does not compare, classify, or rewrite
    model text, and it never invokes the model again.
    """

    if str(getattr(prediction, "termination_reason", "") or "") != "direct_response":
        return False
    allowed_agents = {str(agent_id or "") for agent_id in agent_ids} - {""}
    if not allowed_agents:
        return False
    with transcript._lock:
        if transcript._frozen:
            transcript._audit_late_op("promote_tool_free_response")
            return False
        part = transcript._open_part
        if (
            part is None
            or part.type != "text"
            or transcript._open_field != "next_thought"
            or transcript._open_agent not in allowed_agents
        ):
            return False
        agent_id = transcript._open_agent
        transcript._close_open_text_locked()
        source_key = (agent_id, "next_thought")
        closed = transcript._closed_text.get(source_key)
        if not closed or not any(candidate is part for candidate in transcript._parts):
            return False
        landed = closed.pop()
        if not closed:
            transcript._closed_text.pop(source_key, None)
        transcript._closed_text.setdefault((agent_id, "answer"), []).append(landed)
        part.metadata["signature_field_name"] = "answer"
        transcript._publisher.publish(
            "message.part.updated",
            {
                "turn_id": transcript.turn_id,
                "message_id": transcript.message_id,
                "part_id": part.id,
                "metadata_patch": {"signature_field_name": "answer"},
            },
        )
        return True
