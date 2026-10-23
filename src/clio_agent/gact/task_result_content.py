"""Bounded result reads and exact, committed evidence handoffs; no task lifecycle policy."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from clio_agent import conf
from clio_agent.arc.task_types import AssignmentDetails, AssignmentEvidence, CoreReference
from clio_agent.gact.task_access import TaskAccessDenied, TaskCaller
from clio_agent.gact.task_cursors import decode_cursor
from clio_agent.gact.task_history import TaskHistory

SERIALIZATION = "json-sort-unicode-v1"
_MODES = frozenset({"task_result", "shared_evidence"})


def _tag(*parts: str, length: int) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:length]


def _address_tag(recipient: str, handle: str, mode: str) -> str:
    return _tag(SERIALIZATION, recipient, handle, mode, length=12)


def _content_tag(reference: CoreReference) -> str:
    return _tag(json.dumps(reference.model_dump(mode="json"), sort_keys=True), length=16)


def result_cursor(
    recipient: str, handle: str, reference: CoreReference, mode: str, offset: int = 0
) -> str:
    """Pin a continuation to its recipient, result bytes and read mode, not current task state.

    The cursor is short because models copy it verbatim into every page request
    (#1844): ``v2.<mode>.<offset>.<address tag>.<content tag>``. The tags are
    digests, not credentials; authorization is still checked on every read.
    """
    return ".".join(
        ("v2", mode, str(offset), _address_tag(recipient, handle, mode), _content_tag(reference))
    )


def read_cursor(cursor: str, caller: TaskCaller, handle: str) -> dict[str, Any]:
    """Validate the requested result address before choosing its authorization path."""
    if cursor.startswith("v2."):
        parts = cursor.split(".")
        if (
            len(parts) != 5
            or parts[1] not in _MODES
            or not parts[2].isdigit()
            or parts[3] != _address_tag(caller.session_id, handle, parts[1])
        ):
            raise ValueError("Result cursor belongs to a different read")
        return {"mode": parts[1], "offset": int(parts[2]), "content": parts[4]}
    # Version 1 spelled out the whole reference; delegation inputs committed before
    # the short form still carry it, so it stays readable.
    value = decode_cursor(cursor)
    if (
        value.get("version") != 1
        or value.get("serialization") != SERIALIZATION
        or value.get("recipient") != caller.session_id
        or value.get("handle") != handle
        or value.get("mode") not in _MODES
        or type(value.get("offset")) is not int
        or value["offset"] < 0
    ):
        raise ValueError("Result cursor belongs to a different read")
    reference = CoreReference.model_validate(value.get("reference"))
    return {"mode": value["mode"], "offset": value["offset"], "content": _content_tag(reference)}


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
) -> dict[str, Any]:
    """Read deterministic JSON pages; fragments explicitly require concatenation before parsing."""
    if position and position["content"] != _content_tag(reference):
        raise ValueError("Pinned result reference changed")
    value = history.read_content(reference)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    offset = position.get("offset", 0)
    if offset > len(encoded):
        raise ValueError("Result cursor offset is outside its pinned content")
    bound = conf.resolve(
        "limits.tool_result_chars", env="CLIO_TOOL_RESULT_CHARS", default=12000, cast=conf.as_int
    )
    result: dict[str, Any] = {"result_reference": reference.model_dump(mode="json")}
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
            result_cursor(caller.session_id, handle, reference, mode, end)
            if end < len(encoded)
            else None
        ),
    )
    return result
