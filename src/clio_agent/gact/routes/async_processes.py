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

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from clio_agent.gact.agent_tasks import AgentTask, display_run_name
from clio_agent.gact.agents.tool_instrumentation import declared_tool_title
from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.provenance.child_projection import child_session_lineage
from clio_agent.gact.session_descendants import descendant_session_ids
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.tools.mcp_task_records import TaskRecord

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

__all__ = [
    "mcp_task_display_title",
    "project_session_async_processes",
    "register_async_process_routes",
]

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
        **record.to_wire(),
        "kind": "mcp-task",
        "id": record.task_id,
        "title": mcp_task_display_title(record),
        "live_state": _MCP_TASK_LIVE_STATES.get(record.display_status, record.display_status),
    }


def mcp_task_display_title(record: TaskRecord) -> str:
    """Return a readable server/tool identity without exposing the raw task id."""

    curated = declared_tool_title(record.tool)
    if curated:
        return curated
    if record.tool:
        return record.tool.replace("_", " ").replace("-", " ").strip().title()
    backend_name = record.backend.get("name")
    if isinstance(backend_name, str) and backend_name.strip():
        return f"{backend_name.strip()} task"
    return "Background task"


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

    lineage, _truncated = child_session_lineage(app, session_id)
    lineage_by_session = {str(row["session_id"]): row for row in lineage}
    owner_session_ids = [session_id]
    if include_children:
        owner_session_ids.extend(descendant_session_ids(app, session_id))
    agent_tasks = [
        task
        for owner_session_id in owner_session_ids
        for task in app.state.agent_task_registry.for_parent(owner_session_id)
    ]
    records = app_task_store(app).list()
    from clio_agent.gact.task_backend_identity import relay_mirrors

    mirrored = relay_mirrors(app, agent_tasks, records)
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
        for record in records
        if record.session_id in owner_session_ids and record.key not in mirrored
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
        from clio_agent.gact.task_projection import task_views

        return {
            "processes": [
                row
                for row in task_views(app, sid, include_children=include_children)
                if not row.get("dismissed")
            ]
        }

    @app.get("/v1/sessions/{sid}/async-tasks")
    async def list_tasks(
        sid: str,
        kind: str | None = None,
        status: str | None = None,
        handle: str | None = None,
        tool: str | None = None,
        agent: str | None = None,
        active_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        from clio_agent.gact.task_controls import query_snapshot

        if app.state.sessions.get(sid) is None:
            raise _not_found("session", sid)
        try:
            return query_snapshot(
                app,
                sid,
                kind=kind,
                status=status,
                handle=handle,
                tool=tool,
                agent=agent,
                active_only=active_only,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/sessions/{sid}/async-tasks/cancel")
    async def cancel_tasks(sid: str, body: TaskSelection) -> dict[str, Any]:
        from clio_agent.gact.task_controls import cancel_selected

        if app.state.sessions.get(sid) is None:
            raise _not_found("session", sid)
        return await asyncio.to_thread(cancel_selected, app, sid, body.tasks)

    @app.get("/v1/sessions/{sid}/async-tasks/{handle}/result")
    async def get_result(sid: str, handle: str) -> dict[str, Any]:
        from clio_agent.gact.task_controls import collect_result
        from clio_agent.gact.task_projection import resolve_task

        if app.state.sessions.get(sid) is None:
            raise _not_found("session", sid)
        try:
            return collect_result(app, sid, resolve_task(app, sid, handle))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


class TaskSelection(BaseModel):
    """The same nonempty handle list accepted by agent task controls."""

    tasks: str | list[str] = Field(min_length=1)
