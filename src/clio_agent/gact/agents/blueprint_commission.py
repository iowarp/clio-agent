"""Cross-blueprint commission targeting, context delivery, and ledger events."""

from __future__ import annotations

from typing import Any

SPAWN_AGENT_ARGUMENT = {
    "type": "string",
    "description": "Declared child expert id. Omit when blueprint_id targets an installed blueprint.",
}
SPAWN_BLUEPRINT_ARGUMENT = {
    "type": "string",
    "description": (
        "Optional installed blueprint id to commission. The child activates its root expert and "
        "returns its registered artifact; a supplied agent must match that root."
    ),
}


def resolve_commission_target(
    app: Any, session_id: str, requested_agent: str, blueprint_id: str | None
) -> tuple[str, dict[str, Any] | None, str, str]:
    """Resolve an optional installed-blueprint target for one spawn."""

    target_id = str(blueprint_id or "").strip()
    if not target_id:
        return requested_agent, None, "", ""
    from clio_agent.gact.spawn_context import resolve_installed_blueprint_target  # noqa: PLC0415
    from clio_agent.gact.turn_spawn import SpawnError  # noqa: PLC0415

    parent_session = app.state.sessions.get(session_id)
    workspace_id = str(getattr(parent_session, "workspace_id", "") or "")
    child_id, scope, display_name = resolve_installed_blueprint_target(
        app, target_id, workspace_id=workspace_id
    )
    if requested_agent and requested_agent != child_id:
        raise SpawnError(
            f"blueprint {target_id!r} has root {child_id!r}, not requested expert {requested_agent!r}",
            reason="blueprint_root_mismatch",
        )
    return child_id, scope, display_name, target_id


def completion_context_fields(app: Any, task: Any) -> dict[str, Any]:
    """Return verified artifact context fields for a collected task."""

    if not task.artifact_ref:
        return {}
    from clio_agent.gact.agent_task_artifacts import artifact_context_for_task  # noqa: PLC0415

    context = artifact_context_for_task(app, task)
    return {"artifact_context": context} if context else {}


def collect_commission_artifact(app: Any, parent_session_id: str, task: Any) -> None:
    """Record first use when a parent collects a commissioned task result."""

    from clio_agent.gact.agent_task_artifacts import emit_commission_parent_use  # noqa: PLC0415

    emit_commission_parent_use(app, parent_session_id, task)


def emit_commission_started(
    app: Any,
    session_id: str,
    parent_id: str,
    child_id: str,
    target_id: str,
    display_name: str,
    spawned: Any,
    emit_semantic_event: Any,
    turn_id: str,
    trace_id: str,
) -> None:
    """Publish the commission separately from the ordinary delegation start."""

    if not target_id:
        return
    emit_semantic_event(
        app,
        session_id,
        "blueprint.commission.started",
        turn_id=turn_id,
        trace_id=trace_id,
        status=spawned.status,
        summary=f"{parent_id} commissioned {display_name or target_id}",
        actor={"agent_id": parent_id, "role": "commissioning_parent"},
        subject={"agent_id": child_id, "role": "commissioned_blueprint"},
        blueprint={
            "agent_blueprint_id": target_id,
            "parent_expert": parent_id,
            "child_expert": child_id,
        },
        payload={
            "task_id": spawned.task_id,
            "target_blueprint_id": target_id,
            "target_blueprint_name": display_name,
            "child_session_id": getattr(spawned, "child_session_id", ""),
        },
    )


def emit_commission_artifact_returned(
    app: Any,
    session_id: str,
    parent_id: str,
    child_id: str,
    task: Any,
    emit_semantic_event: Any,
    turn_id: str,
    trace_id: str,
) -> None:
    """Publish a registered artifact return after delegation completion."""

    target_id = str(task.agent_ref.get("blueprint_id") or "")
    if not target_id or not task.artifact_ref:
        return
    emit_semantic_event(
        app,
        session_id,
        "blueprint.commission.artifact_returned",
        turn_id=turn_id,
        trace_id=trace_id,
        status=task.status,
        summary=f"{target_id} returned a registered artifact.",
        actor={"agent_id": child_id, "role": "commissioned_blueprint"},
        subject={"agent_id": parent_id, "role": "commissioning_parent"},
        blueprint={
            "agent_blueprint_id": target_id,
            "parent_expert": parent_id,
            "child_expert": child_id,
        },
        payload={"task_id": task.task_id, "artifact_ref": task.artifact_ref},
    )


__all__ = [
    "SPAWN_AGENT_ARGUMENT",
    "SPAWN_BLUEPRINT_ARGUMENT",
    "collect_commission_artifact",
    "completion_context_fields",
    "emit_commission_artifact_returned",
    "emit_commission_started",
    "resolve_commission_target",
]
