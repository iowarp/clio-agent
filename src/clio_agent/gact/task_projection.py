"""Common task views over the existing session and workload owners."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.routes.async_processes import project_session_async_processes

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
KINDS = frozenset({"Subagent", "MCP", "Download", "Indexing", "Shell"})


def public_task(row: dict[str, Any]) -> dict[str, Any]:
    """Return bounded task metadata, keeping stored results and input answers private."""
    fields = {
        "handle",
        "id",
        "kind",
        "task_kind",
        "tool",
        "title",
        "label",
        "description",
        "description_missing",
        "owner",
        "effective_status",
        "raw_status",
        "status",
        "live_state",
        "progress",
        "created_at",
        "updated_at",
        "supported_actions",
        "result_reference",
        "cancel_requested",
        "connection_freshness",
        "host",
        "placement",
        "root_session_id",
        "owner_session_id",
        "parent_session_id",
        "child_session_id",
        "task_path",
        "handle_id",
        "depth",
        "run_index",
    }
    return {name: value for name, value in row.items() if name in fields}


def task_views(app: Any, sid: str, *, include_children: bool = True) -> list[dict[str, Any]]:
    """Project only the caller's own tasks and descendants before any filtering."""
    rows = project_session_async_processes(app, sid, include_children=include_children)
    store = app_task_store(app)
    records = {row.key: row for row in store.list()}
    from clio_agent.tools.mcp_task_records import TaskKey

    for row in rows:
        subagent = row["kind"] == "agent"
        if subagent:
            handle = row.get("handle_id") or row["id"]
            kind = "Subagent"
            description = row.get("description", "")
            owner = row["parent_session_id"]
        else:
            record = records[TaskKey.from_wire(row["key"])]
            if not record.handle:
                record = replace(record, handle="task_" + uuid4().hex)
                store.put(record)
            handle = record.handle
            kind = record.kind
            description = record.description
            owner = record.session_id
        status = row.get("live_state") or row.get("effective_status") or row.get("status")
        active = status not in TERMINAL
        supervisor = getattr(app.state, "task_supervisor", None)
        cancellable = active and (subagent or handle in getattr(supervisor, "cancellers", {}))
        if subagent:
            cancellable = cancellable or any(
                row["id"] in descendant.get("task_path", [])
                and descendant["id"] != row["id"]
                and (descendant.get("live_state") or descendant.get("status")) not in TERMINAL
                for descendant in rows
            )
        row.update(
            handle=handle,
            task_kind=kind,
            description=description or "Description unavailable for this older task",
            description_missing=not bool(description),
            label=row["title"],
            owner={
                "session_id": owner,
                "agent": row.get("agent_ref", {}).get("requesting_expert_id", "")
                if subagent
                else row.get("owner_agent", ""),
            },
            effective_status=status,
            raw_status=row.get("status"),
            supported_actions=["observe", "wait", "result"] + (["cancel"] if cancellable else []),
            result_reference={"handle": handle, "tool": "get_task_result"},
            connection_freshness=row.get("connection_freshness", "unknown"),
            progress=row.get("backend", {}).get("progress", {}),
        )
    return rows


def resolve_task(app: Any, sid: str, handle: str) -> dict[str, Any]:
    """Resolve an authorized public handle or an unambiguous legacy alias."""
    rows = task_views(app, sid)
    exact = [row for row in rows if row["handle"] == handle]
    matches = exact or [row for row in rows if row["id"] == handle]
    if len(matches) != 1:
        raise ValueError("ambiguous_task" if matches else "unknown_or_unauthorized_task")
    return matches[0]
