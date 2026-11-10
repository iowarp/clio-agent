"""A study's views: the dashboards its sessions attached with ``view`` entries (CT19).

A view is a dashboard the session that wrote the ``view`` entry published
(``publish_dashboard_report``), so only that session revises it. Reading the study's views
folds each standing entry with the latest version of its owner's report. Views attached
before dashboards were (an entry naming a chat surface) still read, from the owner's
transcript projection, and are marked read-only. Nothing here stores anything: the
attachment is the entry in the owner's event log, and the dashboard is its artifact.
"""

from __future__ import annotations

from typing import Any

#: Statuses of a ``view`` entry that keep it listed.
_STANDING = frozenset({"recorded"})


def _dashboard(app: Any, owner: str, report_id: str) -> dict[str, Any] | None:
    """The latest version of ``owner``'s dashboard ``report_id``, or ``None``."""
    from clio_agent.gact.dashboard_reports import (  # noqa: PLC0415
        list_dashboard_reports,
        read_dashboard_report,
    )

    for report in list_dashboard_reports(app, owner):
        if report["id"] == report_id:
            try:
                return {**read_dashboard_report(app, owner, report["artifact_id"]), **report}
            except ValueError:  # its bytes are gone: listed with the typed reason
                return None
    return None


def _legacy_surface(app: Any, owner: str, surface_id: str) -> Any:
    store = getattr(getattr(app, "state", None), "a2ui_store", None)
    record = store.get(owner, surface_id) if store is not None and owner else None
    return record if record is not None and getattr(record, "state", "") != "deleted" else None


def study_views(app: Any, entries: list[dict[str, Any]], root: str) -> list[dict[str, Any]]:
    """Each standing view with its dashboard's latest surface, the root's views first.

    A view whose dashboard (or, for an earlier view, surface) is gone is listed with
    ``surface: None`` and a typed ``reason`` so the tab can say so, never dropped silently.
    """

    rows: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") != "view" or entry.get("status") not in _STANDING:
            continue
        owner = str(entry.get("branch") or "")
        fields = entry.get("fields") or {}
        row: dict[str, Any] = {
            "entry": entry["id"],
            "session_id": owner,
            "title": str(fields.get("title") or ""),
            "purpose": str(fields.get("purpose") or ""),
            "serves": [link["to"] for link in entry.get("links") or []],
            "created_at": entry.get("created_at"),
        }
        report_id = str(fields.get("dashboard_id") or "")
        if report_id:
            report = _dashboard(app, owner, report_id) if owner else None
            surface = (report or {}).get("surface")
            row.update(
                kind="dashboard",
                dashboard_id=report_id,
                artifact_id=(report or {}).get("artifact_id", ""),
                surface_id=str((surface or {}).get("id") or ""),
                surface=surface,
                **({} if surface else {"reason": "view_dashboard_missing"}),
            )
        else:
            surface_id = str(fields.get("surface_id") or "")
            record = _legacy_surface(app, owner, surface_id)
            row.update(
                kind="surface",
                read_only=True,  # attached as a chat surface, before views were dashboards
                surface_id=surface_id,
                surface=record.to_wire() if record is not None else None,
                **({} if record is not None else {"reason": "view_surface_deleted"}),
            )
        rows.append(row)
    rows.sort(key=lambda row: (row["session_id"] != root, str(row["created_at"])))
    return rows


__all__ = ["study_views"]
