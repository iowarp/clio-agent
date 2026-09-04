"""Canonical parent-ledger events for real child-task spawns."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.spawn_group import spawn_group_fields
from clio_agent.gact.agents.spawn_placement import run_handle_fields
from clio_agent.gact.runtime.globals import (
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _emit_semantic_event,
)
from clio_agent.gact.tool_observer import _append_live_assistant_part, _handoff_part_metadata
from clio_agent.gact.types import Part

if TYPE_CHECKING:
    from clio_agent.gact.agents.types import AgentDef


def _started_handoff_part(
    agent_def: AgentDef,
    child_id: str,
    task_text: str,
    depth: int,
    spawned: Any,
    *,
    input_task_ids: list[str] | None = None,
) -> Part:
    """Build the running child handoff appended at its causal launch position.

    ``input_task_ids`` (#1306 final review round, finding N4), when non-empty,
    is stamped as the bounded id LIST ONLY -- never invented as an empty
    sentinel (mirrors ``spawn_group_id``/``group_size``'s own "absent, not
    empty" convention on this same Part) -- so a spawn that forwarded evidence
    shows that fact on the delegation edge without the parent's transcript
    ever carrying the forwarded text itself (that text lives only in the
    CHILD's own task briefing, ``task_text`` here, already the bare task).
    """
    started_row = {
        "agent_id": child_id,
        "parent_id": agent_def.id,
        "status": "running",
        "stage": "delegate.started",
        "question": task_text,
        "depth": depth,
        "run_index": spawned.run_index,
    }
    if input_task_ids:
        started_row["input_task_ids"] = list(input_task_ids)
    started_row.update(spawn_group_fields(spawned))
    handle_fields = run_handle_fields(spawned, child_id)
    return Part(
        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
        type="expert_handoff",
        agent_id=agent_def.id,
        parent_agent=agent_def.id,
        child_agent=child_id,
        stage="delegate.started",
        handle_id=handle_fields["handle_id"],
        run_label=handle_fields["run_label"],
        live_state=handle_fields["live_state"],
        host=handle_fields["host"],
        placement=handle_fields["placement"],
        status="running",
        text=f"{agent_def.id} -> {child_id}",
        metadata={**_handoff_part_metadata(started_row), "stream_source": "live"},
    )


def emit_spawn_started(
    app: Any,
    session_id: str,
    agent_def: AgentDef,
    child_id: str,
    task_text: str,
    depth: int,
    spawned: Any,
    *,
    emit_semantic_event: Callable[..., Any] = _emit_semantic_event,
    append_live_part: Callable[..., Any] = _append_live_assistant_part,
    input_task_ids: list[str] | None = None,
) -> None:
    """Publish one canonical started handoff for any real child-task spawn."""
    emit_semantic_event(
        app,
        session_id,
        "blueprint.delegation.started",
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status="running",
        summary=f"{agent_def.id} spawned {child_id}",
        actor={"agent_id": agent_def.id, "role": "parent_expert"},
        subject={"agent_id": child_id, "role": "child_expert"},
        blueprint={
            "agent_blueprint_id": agent_def.metadata.get("agent_blueprint_id") or "",
            "parent_expert": agent_def.id,
            "child_expert": child_id,
        },
        payload={"run_index": spawned.run_index},
    )
    append_live_part(
        app,
        session_id,
        _started_handoff_part(
            agent_def, child_id, task_text, depth, spawned, input_task_ids=input_task_ids
        ),
    )
