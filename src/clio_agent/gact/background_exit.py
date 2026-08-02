"""Typed UI twin for consumed background-task exit notifications (#1131)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from clio_agent.gact.agents.spawn_placement import run_handle_fields
from clio_agent.gact.parts import Part

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.agent_tasks import AgentTask

_EXIT_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "canceled",
}


def background_exit_part(task: "AgentTask") -> Part:
    """Build one additive ``background_exit`` part from a terminal task.

    Args:
        task: The terminal task whose observe-later notification won the shared
            consumption gate.

    Returns:
        A stable UI-facing part carrying the run handle, task/job identity, exit
        status, and an artifact reference only when the terminal fold supplied one.

    Raises:
        ValueError: If ``task`` is not in a terminal status.
    """

    exit_status = _EXIT_STATUS.get(task.status)
    if exit_status is None:
        raise ValueError(f"background exit requires a terminal task, got {task.status!r}")
    child_id = task.agent_ref.get("expert_id", "")
    parent_id = task.agent_ref.get("requesting_expert_id", "") or "main"
    fields = run_handle_fields(task, child_id)
    return Part(
        id=f"live_background_exit_{uuid.uuid4().hex[:12]}",
        type="background_exit",
        agent_id=child_id,
        parent_agent=parent_id,
        child_agent=child_id,
        handle_id=fields["handle_id"],
        run_label=fields["run_label"],
        live_state=fields["live_state"],
        host=fields["host"],
        placement=fields["placement"],
        task_id=task.task_id,
        job_id=task.task_id,
        exit_status=exit_status,
        artifact_ref=task.artifact_ref,
        status=task.status,
        metadata={"stream_source": "live"},
    )


def emit_background_exit_part(app: "FastAPI", session_id: str, task: "AgentTask") -> Part:
    """Append one typed exit part to the active parent turn and return it.

    This function owns only emission. Callers must first win
    :func:`agent_tasks.consume_notification`; invoking it for an unclaimed task
    would bypass the existing exactly-once gate.
    """

    from clio_agent.gact.tool_observer import _append_live_assistant_part  # noqa: PLC0415

    part = background_exit_part(task)
    _append_live_assistant_part(app, session_id, part)
    return part
