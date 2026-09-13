"""The per-session pending-diff ledger: finalize indexing + guarded snapshots (#1334).

``app.state.pending_diffs`` maps ``session_id -> [row]`` where a row is
``{path, unified_diff, new_content, status, part_id, message_id}``; ``status`` is
``"pending"`` until the diff apply / reject routes flip it. Turn finalize appends rows
here from the turn executor (off the loop) while the routes scan and pop on the loop,
so every touch holds :func:`~clio_agent.gact.runtime.retention.ledger_guard`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from clio_agent.gact.runtime.retention import enforce_list_bound, ledger_guard

if TYPE_CHECKING:
    from fastapi import FastAPI


def index_turn_file_diffs(
    app: "FastAPI", sid: str, parts: Iterable[Any], *, message_id: str
) -> int:
    """Index a finalized turn's ``file_diff`` parts so apply / reject can find them.

    Returns the number of rows appended. ``new_content`` is carried only when the part
    holds content to write (or declares a ``whole`` / ``patch`` edit mode), so a legacy
    or test diff without it gets the wire event but no disk write on apply.
    """

    rows = [
        {
            "path": p.path,
            "unified_diff": p.unified_diff,
            "new_content": (
                p.new_content if p.new_content or p.edit_mode in {"whole", "patch"} else None
            ),
            "status": "pending",
            "part_id": p.id,
            "message_id": message_id,
        }
        for p in parts
        if p.type == "file_diff"
    ]
    with ledger_guard(app):
        bucket = app.state.pending_diffs.setdefault(sid, [])
        bucket.extend(rows)
        enforce_list_bound(app, bucket, "pending_diffs", session_id=sid)
    return len(rows)


def pending_diff_rows(app: "FastAPI", sid: str) -> list[dict[str, Any]]:
    """A guarded snapshot of the session's live rows (the dicts themselves are shared)."""

    with ledger_guard(app):
        return list(app.state.pending_diffs.get(sid, []))


__all__ = ["index_turn_file_diffs", "pending_diff_rows"]
