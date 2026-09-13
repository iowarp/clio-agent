"""Completion payload and terminal handoff projection for spawned agent tasks."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.spawn_group import spawn_group_fields
from clio_agent.gact.agents.spawn_placement import run_handle_fields
from clio_agent.gact.tool_observer import _handoff_part_metadata
from clio_agent.gact.turn_spawn_result import message_text
from clio_agent.gact.types import Part

if TYPE_CHECKING:
    from clio_agent.gact.agents.types import AgentDef


def resolve_verbatim_output(app: Any, task: Any) -> tuple[str, dict[str, str]]:
    """Resolve the full child answer, with an explicit bounded-excerpt fallback."""

    result = task.result or {}
    excerpt = result.get("answer_excerpt", "")
    message_ref = result.get("message_ref", "")
    child_sid = getattr(task, "child_session_id", "")
    if not message_ref or not child_sid:
        return excerpt, {}
    for message in app.state.messages.get(child_sid, []) or []:
        if getattr(message, "id", "") == message_ref:
            return message_text(message), {}
    return excerpt, {
        "output_source": "excerpt_fallback",
        "output_fallback_reason": "child_message_gone",
    }


def completion_payload(app: Any, task: Any) -> dict[str, Any]:
    """Build the delegate.completed payload with the verbatim child answer."""

    result = task.result or {}
    output, markers = resolve_verbatim_output(app, task)
    payload = {
        "agent_id": task.agent_ref.get("expert_id", ""),
        "parent_id": task.agent_ref.get("requesting_expert_id", ""),
        "task_id": task.task_id,
        "run_index": task.run_index,
        "status": task.status,
        "stage": "delegate.completed" if task.status == "completed" else f"delegate.{task.status}",
        "output": output,
        "workflow_state": result.get("workflow_state", {}),
        "message_ref": result.get("message_ref", ""),
        "error_reason": task.error_reason,
        "artifact_ref": task.artifact_ref,
    }
    payload.update(markers)
    return payload


def task_duration_ms(task: Any) -> float:
    """Return child wall-clock duration, or zero for malformed timestamps."""

    try:
        created = datetime.fromisoformat(str(task.created_at))
        updated = datetime.fromisoformat(str(task.updated_at))
    except (TypeError, ValueError):
        return 0.0
    delta_ms = (updated - created).total_seconds() * 1000
    return delta_ms if delta_ms > 0 else 0.0


def return_handoff_part(agent_def: "AgentDef", task: Any, payload: dict[str, Any]) -> Part:
    """Build the terminal return Part appended to the parent transcript."""

    child_id = task.agent_ref.get("expert_id", "")
    return_row = {
        "agent_id": child_id,
        "parent_id": agent_def.id,
        "status": task.status,
        "stage": "delegate.completed",
        "output": payload.get("output", ""),
        "workflow_state": payload.get("workflow_state", {}),
        "error": task.error_reason or "",
        "run_index": task.run_index,
        "artifact_ref": task.artifact_ref,
    }
    return_row.update(spawn_group_fields(task))
    for marker in ("output_source", "output_fallback_reason"):
        if marker in payload:
            return_row[marker] = payload[marker]
    handle_fields = run_handle_fields(task, child_id)
    return Part(
        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=agent_def.id,
        parent_agent=agent_def.id,
        child_agent=child_id,
        stage="delegate.completed",
        handle_id=handle_fields["handle_id"],
        run_label=handle_fields["run_label"],
        live_state=handle_fields["live_state"],
        host=handle_fields["host"],
        placement=handle_fields["placement"],
        status=task.status,
        duration_ms=task_duration_ms(task),
        text=f"{agent_def.id} <- {child_id}",
        metadata={**_handoff_part_metadata(return_row), "stream_source": "live"},
    )


__all__ = [
    "completion_payload",
    "resolve_verbatim_output",
    "return_handoff_part",
    "task_duration_ms",
]
