"""Read-only work presentation projected from retained session records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _todos(value: Any) -> list[dict[str, str]]:
    return [
        {
            "content": str(row.get("content") or ""),
            "status": str(row.get("status") or "pending"),
        }
        for row in value or []
        if isinstance(row, Mapping)
    ]


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
    result["todos"] = _todos(metadata.get("todos", []))
    return result


def retained_work_history(
    messages: list[Any],
    *,
    current_todos: list[dict[str, str]],
    current_schedules: list[Mapping[str, Any]],
    cursor: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """Project previous todo snapshots and schedules from the retained ARC ledger.

    This is a read projection, not another history store. Tool calls/results already live
    in the immutable session message lane; Work folds their structured facts into compact
    previous-state rows while current state remains owned by session metadata and the
    schedule store.
    """

    todo_snapshots: list[dict[str, Any]] = []
    schedules: dict[str, dict[str, Any]] = {}
    calls: dict[str, Mapping[str, Any]] = {}

    for message in sorted(messages, key=lambda row: str(_field(row, "created_at"))):
        created_at = str(_field(message, "created_at"))
        for part in _field(message, "parts", []) or []:
            part_type = str(_field(part, "type"))
            call_id = str(_field(part, "call_id"))
            tool_name = str(_field(part, "tool_name"))
            if part_type == "tool_call" and call_id:
                calls[call_id] = _mapping(_field(part, "input", {}))
                continue
            if part_type != "tool_result":
                continue
            structured = _mapping(_field(part, "structured_content", {}))
            args = calls.get(call_id, {})
            if tool_name == "write_todos":
                items = _todos(structured.get("todos") or args.get("todos") or [])
                if items and (not todo_snapshots or todo_snapshots[-1]["items"] != items):
                    todo_snapshots.append({"id": call_id, "created_at": created_at, "items": items})
                continue
            if tool_name == "cron_create":
                schedule_id = str(structured.get("schedule_id") or "")
                if not schedule_id:
                    continue
                schedules[schedule_id] = {
                    "id": schedule_id,
                    "question": str(args.get("prompt") or "Scheduled turn"),
                    "state": "scheduled",
                    "created_at": created_at,
                    "ended_at": "",
                    "recurring": bool(structured.get("recurring", args.get("recurring", True))),
                    "next_fire_at": str(structured.get("next_fire_at") or ""),
                    "timezone": str(structured.get("timezone") or ""),
                }
                continue
            if tool_name == "cron_delete" and structured.get("deleted") is True:
                schedule_id = str(structured.get("schedule_id") or args.get("schedule_id") or "")
                if not schedule_id:
                    continue
                row = schedules.setdefault(
                    schedule_id,
                    {
                        "id": schedule_id,
                        "question": "Scheduled turn",
                        "state": "deleted",
                        "created_at": "",
                        "ended_at": created_at,
                        "recurring": False,
                        "next_fire_at": "",
                        "timezone": "",
                    },
                )
                row["state"] = "deleted"
                row["ended_at"] = created_at

    if todo_snapshots and todo_snapshots[-1]["items"] == current_todos:
        todo_snapshots.pop()

    active_ids = {str(row.get("id") or "") for row in current_schedules}
    schedule_history = []
    for row in schedules.values():
        if row["id"] in active_ids:
            continue
        if row["state"] == "scheduled":
            row["state"] = "completed" if not row["recurring"] else "stopped"
        schedule_history.append(row)

    todo_rows = list(reversed(todo_snapshots))
    schedule_rows = sorted(
        schedule_history,
        key=lambda row: str(row.get("ended_at") or row.get("created_at") or ""),
        reverse=True,
    )
    return {
        "todo_history": todo_rows[cursor : cursor + limit],
        "todo_history_next_cursor": (cursor + limit if cursor + limit < len(todo_rows) else None),
        "schedule_history": schedule_rows[cursor : cursor + limit],
        "schedule_history_next_cursor": (
            cursor + limit if cursor + limit < len(schedule_rows) else None
        ),
    }
