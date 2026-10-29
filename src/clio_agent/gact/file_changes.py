"""The record of files the agent changed in a session (owner module).

The agent writes files itself with ``fs_write_file`` and ``fs_edit_file``. Each successful
call is recorded here, at the tool observer's completion seam where the whole result is in
hand (the per-turn tool rows are bounded previews and would lose a large diff):

* a row in ``app.state.file_changes[session_id]``, the session's change ledger that
  ``GET /v1/sessions/{sid}/diffs`` lists and ``POST .../diffs/revert`` undoes; and
* a live ``file_diff`` part in the turn's transcript, marked ``applied``, so the change
  shows where it happened. Turn finalize stamps the assistant message id onto the rows
  (:func:`stamp_turn_file_changes`).

A row carries what Undo needs: the sha256 of the file after the write, and either the
previous text (captured at the call boundary, at most
:data:`~clio_agent.tools.fs_edit.UNDO_CAPTURE_MAX_BYTES`) or, for a file the write
created, nothing (Undo deletes it). When the previous text could not be kept the row says
why in ``undo_unavailable_reason`` (a typed reason, never a silent omission). The ledger
lives in memory: after a server restart the transcript still shows each change, but it
can no longer be undone.

Every touch holds :func:`~clio_agent.gact.runtime.retention.ledger_guard`: the observer
appends from tool threads while the routes scan on the loop.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.retention import enforce_list_bound, ledger_guard
from clio_agent.tools.fs_edit import (
    FILE_WRITE_TOOLS,
    UNDO_CHANGED_BEFORE_WRITE,
    UNDO_NOT_CAPTURED,
    PreviousContent,
)
from clio_agent.tools.tool_presentation import PREVIOUS_CONTENT_KEY

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: The retention ledger name (``gact.ledger_retention.file_changes.max``).
FILE_CHANGES_LEDGER = "file_changes"

#: Typed reason logged when a successful write tool returned no change record.
FILE_CHANGE_UNRECORDED = "file_change_unrecorded"


def pop_previous_content(result: Any) -> PreviousContent | None:
    """Take a write tool's pre-call capture off the observer copy of its result.

    Called first at the observer's completion seam so the previous text never reaches a
    wire event, the tool-call ledger or the durable trace.
    """

    if not isinstance(result, dict):
        return None
    captured = result.pop(PREVIOUS_CONTENT_KEY, None)
    return captured if isinstance(captured, PreviousContent) else None


def _change_record(result: Any) -> Mapping[str, Any] | None:
    """The tool's own change record: its ``structuredContent`` (or the plain mapping)."""

    if not isinstance(result, Mapping):
        return None
    record = result.get("structuredContent", result)
    if not isinstance(record, Mapping) or record.get("ok") is not True:
        return None
    if not record.get("path") or not record.get("sha256"):
        return None
    return record


def _undo_state(
    record: Mapping[str, Any], previous: PreviousContent | None
) -> tuple[str | None, str]:
    """Return ``(previous_content, undo_unavailable_reason)`` for one change."""

    if record.get("created") is True:
        return None, ""  # Undo deletes the file the write created.
    if previous is None:
        return None, UNDO_NOT_CAPTURED
    if previous.reason:
        return None, previous.reason
    if previous.sha256 != str(record.get("sha256_before") or ""):
        # The file changed between the capture and the write (a long permission wait):
        # the captured copy is not what the write replaced, so it is never restored.
        return None, UNDO_CHANGED_BEFORE_WRITE
    return previous.content, ""


def record_file_change(
    app: "FastAPI",
    sid: str,
    name: str,
    call_id: str,
    error: str | None,
    result: Any,
    previous: PreviousContent | None,
    agent_id: str = "",
) -> dict[str, Any] | None:
    """Record one successful agent write: a ledger row plus a live ``file_diff`` part.

    ``error`` is the executor's error for the call; a tool that refused (a typed
    ``ok: false`` result) wrote nothing and is not recorded either.
    """

    if name not in FILE_WRITE_TOOLS or error is not None or not sid:
        return None
    record = _change_record(result)
    if record is None:
        logger.warning(
            "file change not recorded reason=%s tool=%s call_id=%s",
            FILE_CHANGE_UNRECORDED,
            name,
            call_id,
        )
        return None
    from clio_agent.gact.parts import Part  # noqa: PLC0415
    from clio_agent.gact.tool_observer import _append_live_assistant_part  # noqa: PLC0415

    previous_content, undo_reason = _undo_state(record, previous)
    change_id = f"chg_{uuid.uuid4().hex[:12]}"
    part = Part(
        id=f"live_{call_id or change_id}_diff",
        type="file_diff",
        agent_id=agent_id,
        path=str(record["path"]),
        unified_diff=str(record.get("unified_diff") or ""),
        status="applied",
        lines_added=int(record.get("lines_added") or 0),
        lines_removed=int(record.get("lines_removed") or 0),
        # The tool call it records rides metadata, never ``call_id``: a part carrying
        # ``call_id`` is read as that call's own row.
        metadata={
            "change_id": change_id,
            "created": record.get("created") is True,
            "tool_call_id": call_id,
        },
    )
    row: dict[str, Any] = {
        "id": change_id,
        "call_id": call_id,
        "tool_name": name,
        "path": part.path,
        "unified_diff": part.unified_diff,
        "lines_added": part.lines_added,
        "lines_removed": part.lines_removed,
        "created": record.get("created") is True,
        "sha256_before": str(record.get("sha256_before") or ""),
        "sha256_after": str(record["sha256"]),
        "previous_content": previous_content,
        "undo_unavailable_reason": undo_reason,
        "status": "applied",
        "part_id": part.id,
        "message_id": "",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    with ledger_guard(app):
        bucket = app.state.file_changes.setdefault(sid, [])
        bucket.append(row)
        enforce_list_bound(app, bucket, FILE_CHANGES_LEDGER, session_id=sid)
    _append_live_assistant_part(app, sid, part)
    app.state.bus.publish(
        Event(
            type="file.diff.applied",
            session_id=sid,
            payload={"session_id": sid, "diff": file_change_to_wire(row)},
        )
    )
    return row


def stamp_turn_file_changes(
    app: "FastAPI", sid: str, parts: Iterable[Any], *, message_id: str
) -> int:
    """Name the assistant message that holds each of this turn's ``file_diff`` parts."""

    part_ids = {p.id for p in parts if getattr(p, "type", "") == "file_diff"}
    if not part_ids:
        return 0
    stamped = 0
    with ledger_guard(app):
        for row in app.state.file_changes.get(sid, []):
            if row.get("part_id") in part_ids and not row.get("message_id"):
                row["message_id"] = message_id
                stamped += 1
    return stamped


def file_change_rows(app: "FastAPI", sid: str) -> list[dict[str, Any]]:
    """A guarded snapshot of the session's rows (the dicts themselves are shared)."""

    with ledger_guard(app):
        return list(app.state.file_changes.get(sid, []))


def file_change_to_wire(row: Mapping[str, Any]) -> dict[str, Any]:
    """The GACT ``diffs`` row: what changed, and whether Undo can restore it."""

    undo_reason = str(row.get("undo_unavailable_reason") or "")
    out: dict[str, Any] = {
        "id": str(row.get("id") or ""),
        "path": str(row.get("path") or ""),
        "status": str(row.get("status") or "applied"),
        "created": bool(row.get("created")),
        "unified_diff": str(row.get("unified_diff") or ""),
        "lines_added": int(row.get("lines_added") or 0),
        "lines_removed": int(row.get("lines_removed") or 0),
        "undo_available": row.get("status") == "applied" and not undo_reason,
    }
    if undo_reason:
        out["undo_unavailable_reason"] = undo_reason
    for key in ("part_id", "message_id", "call_id", "tool_name", "recorded_at", "reverted_at"):
        if row.get(key):
            out[key] = str(row[key])
    return out


__all__ = [
    "FILE_CHANGES_LEDGER",
    "FILE_CHANGE_UNRECORDED",
    "file_change_rows",
    "file_change_to_wire",
    "pop_previous_content",
    "record_file_change",
    "stamp_turn_file_changes",
]
