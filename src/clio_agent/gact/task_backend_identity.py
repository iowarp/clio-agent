"""Durable full backend identity for a relay subagent's opaque local handle."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord


def relay_key(app: Any, task: Any) -> TaskKey | None:
    """Read full retained identity, or resolve an older bare alias only if unambiguous."""
    from clio_agent.gact.agents.relay_invoker_runtime import RELAY_REMOTE_AGENT_TOOL

    child = app.state.sessions.get(task.child_session_id)
    retained = (getattr(child, "metadata", None) or {}).get("relay_task_key")
    if retained:
        key = TaskKey.from_wire(retained)
        if key.session_id != task.parent_session_id:
            raise ValueError("Relay backend identity disagrees with its conversation owner")
        return key
    matches = [
        row.key
        for row in app_task_store(app).list()
        if row.tool == RELAY_REMOTE_AGENT_TOOL
        and row.session_id == task.parent_session_id
        and row.task_id == task.task_id
    ]
    if len(matches) > 1:
        raise ValueError("Relay legacy task identity is ambiguous")
    return matches[0] if matches else None


def relay_mirrors(app: Any, tasks: list[Any], records: list[TaskRecord]) -> set[TaskKey]:
    """Identify mirrored records precisely; never hide another backend's colliding task ID."""
    available = {row.key for row in records}
    keys: set[TaskKey] = set()
    for task in tasks:
        if not task.placement.startswith("relay:"):
            continue
        try:
            key = relay_key(app, task)
        except ValueError:
            continue
        if key in available:
            keys.add(key)
    return keys
