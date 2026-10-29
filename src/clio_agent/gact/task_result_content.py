"""Bounded result reads and exact, committed evidence handoffs; no task lifecycle policy."""

from __future__ import annotations

import json
from typing import Any

from clio_agent import conf
from clio_agent.arc.task_types import AssignmentDetails, AssignmentEvidence, CoreReference
from clio_agent.gact.result_selection import result_keys, select_fields, validate_selection
from clio_agent.gact.task_access import TaskAccessDenied, TaskCaller
from clio_agent.gact.task_cursors import decode_cursor, encode_cursor
from clio_agent.gact.task_history import TaskHistory

SERIALIZATION = "json-sort-unicode-v1"


def result_cursor(
    recipient: str,
    handle: str,
    reference: CoreReference,
    mode: str,
    offset: int = 0,
    select: list[str] | None = None,
) -> str:
    """Pin a continuation to its recipient, result bytes and read mode, not current task state."""
    return encode_cursor(
        {
            "version": 1,
            "serialization": SERIALIZATION,
            "recipient": recipient,
            "handle": handle,
            "mode": mode,
            "reference": reference.model_dump(mode="json"),
            "offset": offset,
            "select": validate_selection(select),
        }
    )


def read_cursor(cursor: str, caller: TaskCaller, handle: str) -> dict[str, Any]:
    """Validate the requested result address before choosing its authorization path."""
    value = decode_cursor(cursor)
    if (
        value.get("version") != 1
        or value.get("serialization") != SERIALIZATION
        or value.get("recipient") != caller.session_id
        or value.get("handle") != handle
        or value.get("mode") not in {"task_result", "shared_evidence"}
        or type(value.get("offset")) is not int
        or value["offset"] < 0
    ):
        raise ValueError("Result cursor belongs to a different read")
    CoreReference.model_validate(value.get("reference"))
    validate_selection(value.get("select"))
    return value


def committed_evidence(history: TaskHistory, caller: TaskCaller, handle: str) -> AssignmentEvidence:
    """Allow only an exact result included in this stable session's confirmed delegation input."""
    current = history.latest_assignment(caller.session_id)
    if current is None or current.owner.human_id != caller.human_id or current.details is None:
        raise TaskAccessDenied("Task result is unavailable")
    details = AssignmentDetails.model_validate(history.read_content(current.details))
    if details.evidence_assignment is None:
        raise TaskAccessDenied("Task result is unavailable")
    identity = "assignment:" + details.evidence_assignment
    committed = history.committed_notification_input(caller.session_id, identity)
    if committed is None:
        raise TaskAccessDenied("Task result is unavailable before delegated input commitment")
    reference = committed.content[committed.notification_ids.index(identity)]
    body = history.read_content(reference)
    if not isinstance(body, dict):
        raise ValueError("Committed delegation content is malformed")
    launch = body.get("launch_context", {})
    if not isinstance(launch, dict):
        raise ValueError("Committed delegation evidence is malformed")
    references = launch.get("input_task_refs")
    if not isinstance(references, list):
        raise ValueError("Committed delegation evidence is malformed")
    confirmed = tuple(AssignmentEvidence.model_validate(pin) for pin in references)
    for pin in details.input_evidence:
        if pin.handle == handle and pin in confirmed:
            return pin
    raise TaskAccessDenied("Task result is unavailable")


def result_page(
    history: TaskHistory,
    caller: TaskCaller,
    handle: str,
    reference: CoreReference,
    mode: str,
    position: dict[str, Any],
    select: list[str] | None = None,
) -> dict[str, Any]:
    """Read deterministic JSON pages; fragments explicitly require concatenation before parsing."""
    if position and CoreReference.model_validate(position["reference"]) != reference:
        raise ValueError("Pinned result reference changed")
    selection = validate_selection(select)
    if position:
        pinned = position.get("select")
        if selection is not None and selection != pinned:
            raise ValueError("Result cursor belongs to a different selection")
        selection = pinned
    original = history.read_content(reference)
    value = select_fields(original, selection)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    offset = position.get("offset", 0)
    if offset > len(encoded):
        raise ValueError("Result cursor offset is outside its pinned content")
    bound = conf.resolve(
        "limits.tool_result_chars", env="CLIO_TOOL_RESULT_CHARS", default=12000, cast=conf.as_int
    )
    result: dict[str, Any] = {"result_reference": reference.model_dump(mode="json")}
    if selection is not None:
        result["result_selection"] = selection
    elif isinstance(original, dict):
        result.update(result_keys(original))
    if not position and (bound <= 0 or len(encoded) <= bound):
        return {**result, "result": value, "next_cursor": None}
    end = min(len(encoded), offset + bound) if bound > 0 else len(encoded)
    result.update(
        result={"preview": encoded[offset:end], "truncated": end < len(encoded)},
        chunk_field="result.preview",
        offset=offset,
        next_offset=end,
        serialization=SERIALIZATION,
        fragments_require_concatenation=True,
        next_cursor=(
            result_cursor(caller.session_id, handle, reference, mode, end, selection)
            if end < len(encoded)
            else None
        ),
    )
    return result
