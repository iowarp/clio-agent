"""A study's views (Addendum B): dashboards its sessions attached with ``view`` entries.

A view is an A2UI surface of the session that wrote the ``view`` entry, so only that
session edits it. Reading the study's views folds each standing entry with its owner's
current surface record. Nothing here stores anything: the attachment is the entry in the
owner's event log and the surface is the owner's transcript projection.
"""

from __future__ import annotations

from typing import Any

#: Statuses of a ``view`` entry that keep it listed.
_STANDING = frozenset({"recorded"})


def study_views(app: Any, entries: list[dict[str, Any]], root: str) -> list[dict[str, Any]]:
    """Each standing view with its owner's current surface, the root's views first.

    A view whose surface its owner has since deleted is listed with ``surface: None``
    and ``reason: "view_surface_deleted"`` so the tab can say so, never dropped silently.
    """

    store = getattr(getattr(app, "state", None), "a2ui_store", None)
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") != "view" or entry.get("status") not in _STANDING:
            continue
        owner = str(entry.get("branch") or "")
        fields = entry.get("fields") or {}
        surface_id = str(fields.get("surface_id") or "")
        record = store.get(owner, surface_id) if store is not None and owner else None
        live = record is not None and getattr(record, "state", "") != "deleted"
        rows.append(
            {
                "entry": entry["id"],
                "session_id": owner,
                "surface_id": surface_id,
                "title": str(fields.get("title") or ""),
                "purpose": str(fields.get("purpose") or ""),
                "serves": [link["to"] for link in entry.get("links") or []],
                "created_at": entry.get("created_at"),
                "surface": record.to_wire() if live and record is not None else None,
                **({} if live else {"reason": "view_surface_deleted"}),
            }
        )
    rows.sort(key=lambda row: (row["session_id"] != root, str(row["created_at"])))
    return rows


__all__ = ["study_views"]
