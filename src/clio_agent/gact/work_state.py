"""Read-only work presentation and retained goal/loop records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def work_record_patch(
    metadata: Mapping[str, Any], kind: str, record: dict[str, Any]
) -> dict[str, Any]:
    """Keep the latest record for each identity without altering execution state."""
    key = f"{kind}_history"
    identity = f"{kind}_id"
    history = {
        row[identity]: dict(row)
        for row in metadata.get(key, [])
        if isinstance(row, Mapping) and row.get(identity)
    }
    previous = metadata.get(kind)
    if isinstance(previous, Mapping) and previous.get(identity):
        history[previous[identity]] = dict(previous)
    if record.get(identity):
        history[record[identity]] = dict(record)
    return {kind: record, key: list(history.values())}


def _work_row(record: Mapping[str, Any], kind: str, current_id: str) -> dict[str, Any]:
    identity = str(record.get(f"{kind}_id") or "")
    if identity != current_id and record.get("active"):
        state = "superseded"
    elif record.get("paused"):
        state = "paused"
    elif record.get("met"):
        state = "completed"
    elif record.get("active"):
        state = "active"
    else:
        state = "stopped"
    return {
        "id": identity,
        "title": str(record.get("condition" if kind == "goal" else "prompt") or ""),
        "state": state,
        "created_at": str(record.get("created_at") or ""),
        "iterations": int(record.get("iters_elapsed" if kind == "goal" else "iteration") or 0),
        "reason": str(record.get("clear_reason" if kind == "goal" else "stop_reason") or ""),
    }


def work_snapshot(metadata: Mapping[str, Any], cursor: int = 0, limit: int = 25) -> dict[str, Any]:
    """Project whitelisted work state with bounded, session-local history pages."""
    result: dict[str, Any] = {"cursor": cursor}
    for kind in ("goal", "loop"):
        current = metadata.get(kind)
        current = current if isinstance(current, Mapping) else {}
        identity = str(current.get(f"{kind}_id") or "")
        history = work_record_patch(metadata, kind, dict(current))[f"{kind}_history"]
        rows = [_work_row(row, kind, identity) for row in reversed(history)]
        result[kind] = _work_row(current, kind, identity) if identity else None
        result[f"{kind}s"] = rows[cursor : cursor + limit]
        result[f"{kind}_next_cursor"] = cursor + limit if cursor + limit < len(rows) else None
    result["todos"] = [
        {"content": str(row.get("content") or ""), "status": str(row.get("status") or "pending")}
        for row in metadata.get("todos", [])
        if isinstance(row, Mapping)
    ]
    return result
