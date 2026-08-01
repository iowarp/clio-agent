"""Uniform runs registry projected over the two existing task stores (#1127).

There is deliberately no registry object and no fifth store. Every read re-sources
local/relay-backed child runs from ``AgentTaskRegistry`` and reconnectable relay job
handles from the installed ``TaskRecordStore``. Matching ids are de-duplicated in
favor of the richer AgentTask row seeded by ``RelayExpertInvoker``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_tasks import AgentTask, persist_agent_task
from clio_agent.tools.mcp_task_records import TaskRecord, resolve_store

if TYPE_CHECKING:
    from fastapi import FastAPI

_RELAY_LIVE_STATES = {
    "working": "running",
    "input_required": "input_required",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _agent_run(task: AgentTask) -> dict[str, Any]:
    """Project one session-backed AgentTask as a human-facing run handle."""

    expert_id = str(task.agent_ref.get("expert_id") or "agent")
    handle_id = task.handle_id or task.task_id
    run_label = task.run_label or f"{expert_id} #{task.run_index + 1}"
    live_state = task.live_state or task.status
    placement = task.placement or "local"
    host = task.host or (placement.split(":", 1)[1] if placement.startswith("relay:") else "local")
    return {
        "handle_id": handle_id,
        "task_id": task.task_id,
        "run_label": run_label,
        "live_state": live_state,
        "status": task.status,
        "host": host,
        "placement": placement,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "detached": task.detached,
        "source": "agent_task",
        "ticker": {
            "state": live_state,
            "updated_at": task.updated_at,
            "path": f"/v1/agent-tasks/{task.task_id}/live",
        },
    }


def _relay_run(record: TaskRecord) -> dict[str, Any]:
    """Project one durable relay/MCP task record not mirrored by an AgentTask."""

    cluster = str(record.backend.get("cluster") or record.key.server_id)
    live_state = _RELAY_LIVE_STATES.get(record.status, record.status)
    return {
        "handle_id": record.task_id,
        "task_id": record.task_id,
        "run_label": record.tool or f"relay run {record.task_id}",
        "live_state": live_state,
        "status": record.status,
        "host": cluster,
        "placement": f"relay:{cluster}",
        "parent_session_id": record.session_id or "",
        "child_session_id": "",
        "created_at": record.created_at,
        "updated_at": "",
        "detached": record.lease_owner is None,
        "source": "relay_job",
        "ticker": {
            "state": live_state,
            "updated_at": "",
            "path": "",
        },
    }


def project_runs(app: "FastAPI") -> list[dict[str, Any]]:
    """List local and relay runs uniformly, newest-created first.

    AgentTask ids are retained even when dismissed so a mirrored relay handle does
    not reappear through the second projection source.
    """

    tasks = app.state.agent_task_registry.snapshot()
    agent_ids = {task.task_id for task in tasks}
    rows = [_agent_run(task) for task in tasks if not task.dismissed]
    rows.extend(
        _relay_run(record)
        for record in resolve_store(None).list()
        if record.task_id not in agent_ids and record.tool == "relay_submit_remote_agent"
    )
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def detach_run(app: "FastAPI", handle_id: str) -> dict[str, Any] | None:
    """Detach a run without cancelling it, using only its existing authoritative store."""

    task = app.state.agent_task_registry.get(handle_id)
    if task is not None:
        if not task.detached:
            task = replace(task, detached=True)
            persist_agent_task(app, task)
        return _agent_run(task)
    for record in resolve_store(None).list():
        if record.task_id == handle_id and record.tool == "relay_submit_remote_agent":
            # A relay record with no active lease is already detached from a driver;
            # detaching never drops or cancels its reconnect handle.
            return {**_relay_run(record), "detached": True}
    return None


def dismiss_run(app: "FastAPI", handle_id: str) -> bool:
    """Hide one run while leaving execution untouched; drop relay-only settled handles."""

    task = app.state.agent_task_registry.get(handle_id)
    if task is not None:
        if not task.dismissed:
            persist_agent_task(app, replace(task, dismissed=True))
        return True
    store = resolve_store(None)
    matches = [
        record
        for record in store.list()
        if record.task_id == handle_id and record.tool == "relay_submit_remote_agent"
    ]
    for record in matches:
        store.drop(record.key)
    return bool(matches)
