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
from clio_agent.tools.mcp_task_records import TERMINAL_TASK_STATES, TaskRecord, resolve_store

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
    not reappear through the second projection source. #1205 review item 3: widened
    the SAME way ``dismiss_run`` was widened — ANY non-agent-task ``TaskRecord``
    (``jarvis_run`` etc.), not only the relay-agent-mirroring
    ``relay_submit_remote_agent`` records this originally covered — so a RETAINED
    settled mcp-task (#1205 2nd round: kept until an explicit dismiss, never
    auto-dropped at settle) is actually reachable through the SAME listing its
    dismiss control (``POST /v1/runs/{handle_id}/dismiss``) targets. Without this
    half, retention is an unbounded, unclearable accumulation in ``sessions.json``:
    a settled record nobody can ever see or dismiss through this surface.
    """

    tasks = app.state.agent_task_registry.snapshot()
    agent_ids = {task.task_id for task in tasks}
    rows = [_agent_run(task) for task in tasks if not task.dismissed]
    rows.extend(
        _relay_run(record)
        for record in resolve_store(None).list()
        if record.task_id not in agent_ids
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
    """Hide one run while leaving execution untouched.

    An ``AgentTask``-backed run is hidden via its ``dismissed`` field — never
    dropped, so ``for_parent``/``project_runs`` keep returning the row (a
    dismissed run is hidden by the CLIENT's own filter, not erased server-side).
    A durable MCP/relay ``TaskRecord`` has no such field: #1205's retention
    design (2nd round) keeps a settled record in the store with its terminal
    status until this explicit action, so dismissing one is the ONE way to make
    it stop appearing — this call drops it for real. Matches ANY tool now, not
    only the relay-agent-mirroring ``relay_submit_remote_agent`` records this
    originally covered — the session-scoped async-processes tray (#1205)
    surfaces every non-agent-task record (``jarvis_run`` etc.), not just that one.

    Two invariants #1205 review (3rd round) holds this to:

    * **Terminality guard (BLOCKING).** A live (non-terminal) ``TaskRecord`` is
      NEVER dropped here — dropping it would delete the only durable local handle
      to a still-running remote task (the exact crash-recovery guarantee
      ``mcp_task_store.py``'s own module contract exists to protect; the old
      tool-name filter only incidentally shielded this by accident, not by
      design). A dismiss request against a non-terminal task is refused
      (``False``), same shape as "no match" — it never partially acts.
    * **Composite-key precision (BLOCKING).** ``handle_id`` is a bare task id, but
      the durable identity is the COMPOSITE ``(server_id, session_id, task_id)``
      — two different backends can legitimately mint the same task id
      (``mcp_task_records.py``'s own module contract; ``cancel_task``'s docstring
      states the identical invariant and ``test_cancel_stamps_only_the_named_identity``
      guards it there). This resolves to at most ONE matching record and drops
      only THAT record's own composite key — never a blanket sweep of every
      record merely sharing the bare id, which would delete an unrelated
      backend's live task as collateral damage.
    """

    task = app.state.agent_task_registry.get(handle_id)
    if task is not None:
        if not task.dismissed:
            persist_agent_task(app, replace(task, dismissed=True))
        return True
    store = resolve_store(None)
    match = next((record for record in store.list() if record.task_id == handle_id), None)
    if match is None or match.status not in TERMINAL_TASK_STATES:
        return False
    store.drop(match.key)
    return True
