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
            }
        )
    return rows


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
