"""Session-scoped async-processes projection: agents + MCP tasks together (#1205).

``GET /v1/sessions/{sid}/agent-tasks`` (``routes/agent_tasks.py``) only ever
projected spawned-child ``AgentTask`` rows. Durable non-agent MCP/relay task
records (#1115's ``TaskRecord``, e.g. a ``jarvis_run`` call) are persisted on the
SAME session's ``metadata["mcp_tasks"]`` (``gact/mcp_task_store.py``) but no route
scoped to a session ever read them, and the tray has no way to tell the two kinds
apart. This module adds ONE new sibling route returning both projections unioned,
each row carrying a ``kind`` discriminator (``"agent"`` | ``"mcp-task"``) so the UI
can render an agent row as a center-focus push and an mcp-task row as a read-only
right-column peek without a second fetch.

No new store: this is a pure read-side union over ``AgentTaskRegistry.for_parent``
and the installed ``TaskRecordStore``, exactly like ``run_registry.py``'s
``project_runs`` unions the same two stores for the (unrelated, global) run-history
surface. Live refresh is the existing per-session SSE channel — ``mcp_task_events.py``
publishes onto it on every durable write; no second SSE route.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.agent_tasks import AgentTask, descendant_session_ids, display_run_name
from clio_agent.gact.provenance.child_projection import child_session_lineage
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.tools.mcp_task_records import TaskRecord, resolve_store

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

__all__ = ["project_session_async_processes", "register_async_process_routes"]

# Mirrors run_registry.py's ``_RELAY_LIVE_STATES`` intentionally rather than
# importing it: that mapping is private to the (unrelated) global run-history
# projection and the two are free to diverge without a shared-constant coupling.
_MCP_TASK_LIVE_STATES: dict[str, str] = {
    "working": "running",
    "input_required": "input_required",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _not_found(kind: str, ident: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="not_found",
                message=f"{kind} not found: {ident}",
                details={f"{kind}_id": ident},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def _agent_process(task: AgentTask) -> dict[str, Any]:
    """Project one spawned-child ``AgentTask`` as a ``kind="agent"`` row."""

    expert_id = str(task.agent_ref.get("expert_id") or "agent")
    return {
        "kind": "agent",
        "id": task.task_id,
        "title": display_run_name(expert_id, task.run_index, task.run_label),
        **asdict(task),
    }


def _mcp_task_process(record: TaskRecord) -> dict[str, Any]:
    """Project one durable ``TaskRecord`` as a ``kind="mcp-task"`` row.

    ``live_state`` is derived from :attr:`TaskRecord.display_status` (#1236),
    not the raw wire ``status`` -- a task delivered with ``isError: true`` must
    show as ``failed`` here too, matching the SSE event type the same record
    publishes. ``record.to_wire()`` still carries BOTH the raw ``status`` and
    the honest ``effective_status``/``effective_status_reason`` -- nothing is
    hidden, only ``live_state`` picks a primary.
    """

    return {
        "kind": "mcp-task",
        "id": record.task_id,
        "title": record.tool or f"mcp task {record.task_id}",
        "live_state": _MCP_TASK_LIVE_STATES.get(record.display_status, record.display_status),
        **record.to_wire(),
    }


def project_session_async_processes(
    app: "FastAPI", session_id: str, *, include_children: bool = True
) -> list[dict[str, Any]]:
    """List every async process (spawned agent OR durable MCP task) for one session.

    Newest-created first, matching ``run_registry.project_runs``'s ordering
    convention. ``TaskRecord`` rows whose ``task_id`` already has an ``AgentTask``
    counterpart (a ``relay_submit_agent`` spawn) are excluded — they are the
    SAME task, already returned once as ``kind="agent"``; this is the identical
    dedupe idiom ``project_runs`` uses.
    """

    lineage = child_session_lineage(app, session_id)
    lineage_by_session = {str(row["session_id"]): row for row in lineage}
    owner_session_ids = [session_id]
    if include_children:
        owner_session_ids.extend(descendant_session_ids(app, session_id))
    agent_tasks = [
        task
        for owner_session_id in owner_session_ids
        for task in app.state.agent_task_registry.for_parent(owner_session_id)
    ]
    agent_task_ids = {task.task_id for task in agent_tasks}
    rows = []
    for task in agent_tasks:
        owner = lineage_by_session.get(task.child_session_id, {})
        rows.append(
            {
                **_agent_process(task),
                "root_session_id": session_id,
                "owner_session_id": task.child_session_id,
                "task_path": list(owner.get("task_path") or [task.task_id]),
            }
        )
    rows.extend(
        {
            **_mcp_task_process(record),
            "root_session_id": session_id,
            "owner_session_id": record.session_id,
            "task_path": list(lineage_by_session.get(record.session_id, {}).get("task_path") or []),
        }
        for record in resolve_store(None).list()
        if record.session_id in owner_session_ids and record.task_id not in agent_task_ids
    )
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def register_async_process_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the session-scoped async-processes read route on ``app``."""

    del deps  # symmetry with the other register_*_routes; state is on app.state

    @app.get("/v1/sessions/{sid}/async-processes")
    async def list_session_async_processes(
        sid: str, include_children: bool = True
    ) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _not_found("session", sid)
        return {
            "processes": project_session_async_processes(
                app, sid, include_children=include_children
            )
        }
