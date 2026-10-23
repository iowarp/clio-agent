"""Non-consuming transcript views over conversation tasks and their existing owners."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from clio_agent import conf
from clio_agent.arc.task_types import TaskSnapshot
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_cursors import decode_cursor, encode_cursor
from clio_agent.gact.tool_observation import CollectedTaskResult

CONTEXT_VIEWS = frozenset({"transcript", "summary", "artifacts"})


def _timestamp(value: str) -> datetime:
    """Compare retained timestamps by instant rather than timezone spelling."""
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Task history contains an unqualified timestamp")
    return stamp


def task_messages(app: Any, caller: TaskCaller, task: TaskSnapshot, history: str) -> list[Any]:
    """Select original assignment rows; session history requires stable read rights."""
    access = TaskAccess(app.state.task_history, caller)
    access.require(task.handle, "read")
    if task.kind not in {"Session", "Subagent"}:
        raise ValueError("Transcript views require a Session or Subagent task")
    if history not in {"assignment", "session"}:
        raise ValueError("History must be assignment or session")
    if history == "session" and task.owner.session_id not in access.session_candidates("read"):
        raise ValueError("Stable session history is unavailable for this operation")
    rows = list(app.state.messages.get(task.owner.session_id, ()))
    current = app.state.task_history.latest_assignment(task.owner.session_id)
    live_id = (
        getattr(app.state, "live_assistant_message_ids", {}).get(task.owner.session_id)
        if current is not None and current.terminal_at is None
        else None
    )
    # An unfinished frame is mutable even when its message header is retained.
    # Keep it outside the continuation cursor until its owner seals the frame.
    rows = [row for row in rows if row.id != live_id]
    if history == "session":
        return rows
    start = _timestamp(task.created_at)
    end = _timestamp(task.terminal_at) if task.terminal_at else None
    turns = app.state.task_history.assignment_turns(task.handle)
    return [
        row
        for row in rows
        if row.id == task.invocation_id
        or row.turn_id == task.invocation_id
        or row.turn_id in turns
        or (
            not row.turn_id
            and _timestamp(row.created_at) >= start
            and (end is None or _timestamp(row.created_at) < end)
        )
    ]


def message_search_window(
    after: str | None, before: str | None
) -> tuple[datetime | None, datetime | None]:
    """Validate an inclusive/exclusive message window and normalize its instants."""
    bounds: list[datetime | None] = []
    for name, value in (("message_after", after), ("message_before", before)):
        if value is None:
            bounds.append(None)
            continue
        try:
            if not isinstance(value, str):
                raise ValueError("Timestamp must be a string")
            bounds.append(_timestamp(value).astimezone(timezone.utc))
        except (ValueError, OverflowError) as error:
            raise ValueError(f"{name} must be a timezone-aware ISO 8601 timestamp") from error
    start, end = bounds
    if start is not None and end is not None and start >= end:
        raise ValueError("message_after must be earlier than message_before")
    return start, end


def matches_task_text(
    app: Any,
    caller: TaskCaller,
    task: TaskSnapshot,
    query: str | None,
    *,
    message_after: datetime | None = None,
    message_before: datetime | None = None,
) -> bool:
    """Match explicit search text only after the task's content authorization."""
    TaskAccess(app.state.task_history, caller).require(task.handle, "read")
    from clio_agent.gact.runtime.session_recall_search import _recall_search_terms

    terms = _recall_search_terms(query) if query is not None else []
    if query is not None and not terms:
        raise ValueError("Task query must contain searchable text")
    windowed = message_after is not None or message_before is not None
    text = [] if windowed else [task.label, task.description or ""]
    if task.kind in {"Session", "Subagent"}:
        messages = task_messages(app, caller, task, "assignment")
        if windowed:
            messages = [
                row
                for row in messages
                if (message_after is None or _timestamp(row.created_at) >= message_after)
                and (message_before is None or _timestamp(row.created_at) < message_before)
            ]
            if not messages:
                return False
        text.extend(
            part.text
            for row in messages
            for part in row.parts
            if part.type in {"text", "thinking", "error"}
        )
    elif windowed:
        return False
    haystack = "\n".join(text).casefold()
    return all(term in haystack for term in terms)


def _summary_rows(app: Any, task: TaskSnapshot, messages: list[Any]) -> list[dict[str, Any]]:
    """Expose actual recorded summaries/outcomes with their source and coverage."""
    from clio_agent.gact.summarization_record import as_summarization

    rows: list[dict[str, Any]] = []
    for message in messages:
        for part in message.parts:
            summary = as_summarization(part)
            if summary is not None:
                rows.append(
                    {
                        "id": "summary:" + part.id,
                        "part_id": part.id,
                        "kind": "recorded_compaction_summary",
                        "message_id": message.id,
                        "text": summary.text,
                        "covered_message_ids": list(summary.compacted_message_ids),
                        "source_ids": list(summary.derived_from),
                        "whole_session": False,
                    }
                )
    if task.result is not None:
        rows.append(
            {
                "id": "result:" + task.result.content_id,
                "kind": "recorded_assignment_outcome",
                "handle": task.handle,
                "result_reference": task.result.model_dump(mode="json"),
                "result": app.state.task_history.read_content(task.result),
                "whole_session": False,
            }
        )
    return rows


def _artifact_rows(app: Any, task: TaskSnapshot, messages: list[Any]) -> list[dict[str, Any]]:
    """List only versions explicitly linked in this authorized transcript."""
    from clio_agent.gact.artifacts.registry import get_registry
    from clio_agent.gact.routes.artifacts import _version_wire

    registry = get_registry(app)
    identities = dict.fromkeys(
        str(part.metadata.get("artifact_id") or "")
        for message in messages
        for part in message.parts
        if part.type == "resource_link" and part.server_id == "clio-artifacts"
    )
    rows: list[dict[str, Any]] = []
    rows.extend(
        {
            "id": "unresolved:" + message.id + ":" + part.id,
            "artifact_id": None,
            "message_id": message.id,
            "part_id": part.id,
            "available": False,
        }
        for message in messages
        for part in message.parts
        if part.type == "resource_link"
        and part.server_id == "clio-artifacts"
        and not part.metadata.get("artifact_id")
    )
    for identity in identities:
        found = registry.get_by_artifact_id(identity)
        if found is None:
            if identity:
                rows.append({"id": identity, "artifact_id": identity, "available": False})
            continue
        record, version = found
        source = app.state.sessions.get(task.owner.session_id)
        if (
            source is None
            or record.workspace_id != source.workspace_id
            or version.producer.get("session_id") != task.owner.session_id
        ):
            rows.append({"id": identity, "artifact_id": identity, "available": False})
            continue
        rows.append({"id": identity, **_version_wire(record.workspace_id, record.name, version)})
    return rows


def _page(
    rows: list[dict[str, Any]], position: dict[str, Any], limit: int, bound: int | None
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """Preserve typed rows, using exact JSON fragments when a row exceeds the budget."""
    anchor, offset = position.get("row"), position.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise ValueError("Task context cursor offset is invalid")
    if position and (
        not isinstance(anchor, str) or not isinstance(position.get("source_sha"), str)
    ):
        raise ValueError("Task context cursor source version is missing")
    start = next((i for i, row in enumerate(rows) if row["id"] == anchor), -1) if anchor else 0
    if start < 0:
        raise ValueError("Task context source changed; restart this view")
    encoded_rows = [
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows
    ]
    source_prefix = hashlib.sha256()
    for encoded in encoded_rows[:start]:
        source_prefix.update(encoded.encode())
        source_prefix.update(b"\n")
    if position and position.get("prior_sha") != source_prefix.hexdigest():
        raise ValueError("Task context source changed; restart this view")
    result: list[dict[str, Any]] = []
    remaining = bound
    for index in range(start, len(rows)):
        row = rows[index]
        encoded = encoded_rows[index]
        begin = offset if index == start else 0
        source_sha = hashlib.sha256(encoded.encode()).hexdigest()
        if (
            begin > len(encoded)
            or (index == start and position.get("source_sha") not in {None, source_sha})
            or (
                index == start
                and position.get("prefix")
                and hashlib.sha256(encoded[:begin].encode()).hexdigest() != position["prefix"]
            )
        ):
            raise ValueError("Task context source changed; restart this view")
        if begin == len(encoded):
            source_prefix.update(encoded.encode())
            source_prefix.update(b"\n")
            continue
        if len(result) >= limit or (remaining is not None and remaining <= 0):
            return (
                result,
                {
                    "row": row["id"],
                    "offset": begin,
                    "source_sha": source_sha,
                    "prior_sha": source_prefix.hexdigest(),
                },
                True,
            )
        text = encoded[begin:] if remaining is None else encoded[begin : begin + remaining]
        end = begin + len(text)
        result.append(
            row
            if begin == 0 and end == len(encoded)
            else {
                "id": row["id"],
                "encoding": "json",
                "offset": begin,
                "content": text,
                "complete": end == len(encoded),
            }
        )
        if remaining is not None:
            remaining -= len(text)
        offset = 0
        position = {
            "row": row["id"],
            "offset": end,
            "source_sha": source_sha,
            "prior_sha": source_prefix.hexdigest(),
            "prefix": hashlib.sha256(encoded[:end].encode()).hexdigest(),
        }
        if end < len(encoded):
            return result, position, True
        source_prefix.update(encoded.encode())
        source_prefix.update(b"\n")
    return result, position, False


def _live_preview(
    app: Any, task: TaskSnapshot, history: str, limit: int, bound: int | None
) -> dict[str, Any] | None:
    """Peek at the current frame without putting mutable parts in a durable cursor."""
    from clio_agent.gact.live_edge import overlay_in_flight_part

    sid = task.owner.session_id
    current = app.state.task_history.latest_assignment(sid)
    if (
        current is None
        or current.terminal_at is not None
        or (history == "assignment" and current.handle != task.handle)
    ):
        return None
    identity = getattr(app.state, "live_assistant_message_ids", {}).get(sid)
    parts = list(getattr(app.state, "live_assistant_parts", {}).get(sid, ()))
    if not identity or not parts:
        return None
    parts = overlay_in_flight_part(app, sid, parts)
    rows = []
    shortened = False
    for part in reversed(parts[-limit:]):
        row = part.to_wire()
        text = row.get("text")
        if isinstance(text, str) and bound is not None and len(text) > bound:
            start = len(text) - bound
            row["text"] = text[start:]
            row["metadata"] = {
                **row.get("metadata", {}),
                "live_text_range": {"start": start, "total": len(text)},
            }
            shortened = True
        rows.append(row)
    page, _, truncated = _page(rows, {}, limit, bound)
    return {
        "message_id": identity,
        "source_handle": current.handle,
        "mutable": True,
        "part_order": "newest_first",
        "parts": page,
        "truncated": truncated or shortened or len(parts) > limit,
    }


def read_task_context(
    app: Any,
    caller: TaskCaller,
    handles: tuple[str, ...],
    view: str,
    history: str = "assignment",
    cursor: str | None = None,
    limit: int = 50,
    artifact_id: str | None = None,
) -> CollectedTaskResult:
    """Read task source context without acknowledging mail, results or model input."""
    if view not in CONTEXT_VIEWS or not 1 <= limit <= 200:
        raise ValueError("Invalid task context view or page limit")
    address = {
        "kind": "task_context",
        "recipient": caller.session_id,
        "human": caller.human_id,
        "handles": list(handles),
        "view": view,
        "history": history,
        "artifact_id": artifact_id,
    }
    previous = decode_cursor(cursor)
    if previous and any(previous.get(key) != value for key, value in address.items()):
        raise ValueError("Task context cursor belongs to a different view or caller")
    positions = previous.get("positions", {})
    if not isinstance(positions, dict) or any(
        key not in handles or not isinstance(value, dict) for key, value in positions.items()
    ):
        raise ValueError("Task context cursor positions are invalid")
    bound = conf.resolve(
        "limits.tool_result_chars", env="CLIO_TOOL_RESULT_CHARS", default=12000, cast=conf.as_int
    )
    pages, readable, more = [], [], False
    remaining = bound if bound > 0 else None
    for handle in handles:
        try:
            task = TaskAccess(app.state.task_history, caller).require(handle, "read")
            messages = task_messages(app, caller, task, history)
            rows = (
                [message.to_wire() for message in messages]
                if view == "transcript"
                else _summary_rows(app, task, messages)
                if view == "summary"
                else _artifact_rows(app, task, messages)
            )
            if artifact_id is not None:
                if (
                    view != "artifacts"
                    or len(handles) != 1
                    or not any(row["id"] == artifact_id for row in rows)
                ):
                    raise ValueError("Linked artifact is unavailable for this task")
                from clio_agent.gact.task_artifact_view import artifact_page

                selected = next(row for row in rows if row["id"] == artifact_id)
                if selected.get("available") is False:
                    raise ValueError("Linked artifact is unavailable for this task")
                prior = positions.get(handle, {})
                if prior and prior.get("sha256") != selected["sha256"]:
                    raise ValueError("Artifact content changed; restart this view")
                body = artifact_page(
                    app,
                    artifact_id,
                    positions.get(handle, {}).get("offset", 0),
                    bound,
                    workspace_id=selected["workspace_id"],
                    producer_session=task.owner.session_id,
                    sha256=selected["sha256"],
                )
                page, position, pending = (
                    [body],
                    {"offset": body["next_offset"], "sha256": selected["sha256"]},
                    body["has_more"],
                )
            else:
                page, position, pending = _page(rows, positions.get(handle, {}), limit, remaining)
                if remaining is not None:
                    remaining = max(0, remaining - len(json.dumps(page, ensure_ascii=False)))
            live = (
                _live_preview(app, task, history, limit, remaining)
                if view == "transcript"
                else None
            )
            if live is not None and remaining is not None:
                remaining = max(0, remaining - len(json.dumps(live["parts"], ensure_ascii=False)))
            TaskAccess(app.state.task_history, caller).require(handle, "read")
            sources = [handle]
            if live is not None:
                sources.append(live["source_handle"])
            if history == "session":
                sources.extend(
                    row.handle
                    for key in app.state.task_history.task_order("session", task.owner.session_id)
                    if (row := app.state.task_history.task_snapshot(key[1])) is not None
                    and row.kind in {"Session", "Subagent"}
                )
            for source in sources:
                TaskAccess(app.state.task_history, caller).require(source, "read")
            positions[handle] = position
            pages.append(
                {
                    **TaskAccess(app.state.task_history, caller).view(task),
                    "view": view,
                    "items": page,
                    "has_more": pending,
                    "summary_available": bool(rows) if view == "summary" else None,
                    "live_snapshot": live,
                }
            )
            readable.extend(sources)
            more = more or pending
        except ValueError as error:
            pages.append({"handle": handle, "error": str(error)})
        except HTTPException as error:
            pages.append(
                {"handle": handle, "error": error.detail, "status_code": error.status_code}
            )
    return CollectedTaskResult(
        {
            "tasks": pages,
            "view": view,
            "history": history,
            "cursor": encode_cursor({**address, "positions": positions}),
            "has_more": more,
        },
        (),
        tuple(readable),
        collect=False,
    )


def session_activity_messages(app: Any, caller: TaskCaller, session_id: str) -> list[Any]:
    """Authorize stable history even for legacy sessions without an assignment."""
    if session_id not in TaskAccess(app.state.task_history, caller).session_candidates("read"):
        raise ValueError("Session history is unavailable for this operation")
    assignment = app.state.task_history.latest_assignment(session_id)
    messages = (
        task_messages(app, caller, assignment, "session")
        if assignment is not None
        else list(app.state.messages.get(session_id, ()))
    )
    if session_id not in TaskAccess(app.state.task_history, caller).session_candidates("read"):
        raise ValueError("Session history is unavailable for this operation")
    return messages


def search_session_alias(
    app: Any, session_id: str, *, query: str, scope: str = "session", limit: int = 20
) -> Any:
    """Retain the shipped search projection over the shared task access policy."""
    from clio_agent.gact.runtime.session_recall_search import _recall_search_response
    from clio_agent.gact.task_access import TaskAccess
    from clio_agent.gact.workspace_scope import GLOBAL_WORKSPACE_ID

    session = app.state.sessions.get(session_id)
    if session is None:
        raise ValueError("Session unavailable")
    readable = TaskAccess(
        app.state.task_history, app.state.conversations.caller(session_id)
    ).session_candidates("read")
    selected_scope = {
        "user": "global",
        "user_global": "global",
        "workspace": "current_workspace",
        "cross_session": "current_workspace",
    }.get(scope, scope)
    if selected_scope not in {"session", "current_workspace", "global", "authorized"}:
        raise ValueError("Memory scope must be session, current_workspace, global or authorized")
    response = _recall_search_response(
        app,
        query=query,
        limit=limit,
        session_id=session_id if selected_scope == "session" else "",
        include_cross_session=selected_scope != "session",
        workspace_id=GLOBAL_WORKSPACE_ID
        if selected_scope == "global"
        else session.workspace_id
        if selected_scope == "current_workspace"
        else "",
        allowed_session_ids={session_id}
        if selected_scope == "session"
        else readable - {session_id}
        if selected_scope == "current_workspace"
        else readable,
    )
    return response.model_copy(
        update={
            "metadata": {
                **response.metadata,
                "policy_decision": "allow_core_relationship",
                "policy_scope": selected_scope,
            }
        }
    )
