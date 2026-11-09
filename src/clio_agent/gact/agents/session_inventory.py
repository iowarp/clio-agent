"""What a session's workspace holds, stated as session facts.

The connected data sources and the uploaded resources are facts CLIO already holds
when a turn starts. An agent learned them only by calling ``connected_data_status``
and ``workspace_resource_list``; on X1 the model's first step spent two calls on
them (2026-11). They now ride ``session_facts`` (``session_facts_sources``,
``session_facts_resources``), which is offered again when they change.

Reads only: the records are read as stored, never materialized or refreshed here.
"""

from __future__ import annotations

from typing import Any

#: Newest resources named in the facts; the total is always stated.
RESOURCES_NAMED = 10


def source_facts(app: Any, workspace_id: str) -> list[dict[str, Any]]:
    """The workspace's connected sources, as ``session_facts_sources`` names them."""
    service = getattr(getattr(app, "state", None), "connected_storage", None)
    if service is None or not workspace_id:
        return []
    rows = []
    for record in service.sources(workspace_id):
        source = record.source
        if source.owner.clio_id != service.store.clio_id:
            continue  # another CLIO's source is never described here
        rows.append(
            {
                "id": source.id,
                "label": source.label or source.id,
                "provider": source.provider,
                "connected": bool(record.connected),
                "access": "read-only" if record.linked_access == "read_only" else "editable",
                "path": source.local_path or "",
                "operations": running_operations(app, source.id),
            }
        )
    return rows


#: Source operation states that are still in flight.
_RUNNING_STATES = frozenset({"queued", "running", "cancellation_requested"})


def running_operations(app: Any, source_id: str) -> list[dict[str, str]]:
    """A source's link/refresh/download tasks still running, named by their task handle.

    A person can link a folder and type at once: the agent must know the indexing has
    not finished, and which handle to ``wait_tasks`` on, instead of reading a partial
    folder. Best effort: facts never fail a turn.
    """
    from clio_agent.gact.storage.task_controls import source_operation_views  # noqa: PLC0415

    if getattr(getattr(app, "state", None), "task_history", None) is None:
        return []
    try:
        views = source_operation_views(app, source_id)
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "handle": str(view["task_handle"]),
            "kind": "download" if view.get("kind") == "materialize" else "indexing",
            "state": str(view["state"]),
        }
        for view in views
        if view.get("task_handle") and view.get("state") in _RUNNING_STATES
    ]


def resource_facts(app: Any, workspace_id: str) -> tuple[list[dict[str, Any]], int]:
    """The newest uploaded resources and how many there are."""
    store = getattr(getattr(app, "state", None), "resource_store", None)
    if store is None or not workspace_id:
        return [], 0
    rows = store.list(workspace_id)
    named = [
        {
            "id": row.id,
            "name": row.name,
            "mime": row.detected_mime or row.claimed_mime or "unknown type",
            "size": row.received_size or row.declared_size,
            "state": row.state,
        }
        for row in rows[:RESOURCES_NAMED]
    ]
    return named, len(rows)
