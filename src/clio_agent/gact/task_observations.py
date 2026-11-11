"""Pure bounded observation pages over authorized task snapshots and Core output."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from clio_agent.gact.task_access import TaskAccess, TaskAccessDenied, TaskCaller
from clio_agent.gact.task_cursors import encode_cursor
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_output_pages import output_page, validate_output_cursor, workflow_state
from clio_agent.tools.result_room import result_room


def read_observations(
    history: TaskHistory,
    caller: TaskCaller,
    handles: tuple[str, ...],
    previous: dict[str, Any],
    row_limit: int | None = None,
    include_state: bool = False,
    pattern: re.Pattern[str] | None = None,
) -> dict[str, Any]:
    """Advance only returned rows/bytes; preserve omitted positions in the opaque cursor."""
    if row_limit is not None and (type(row_limit) is not int or row_limit < 1):
        raise ValueError("Observation limit must be positive")
    if previous and (
        previous.get("version") != 2
        or previous.get("recipient") != caller.session_id
        or previous.get("handles") != list(handles)
    ):
        raise ValueError("Observation cursor belongs to a different read or history epoch")
    if not isinstance(previous.get("positions", {}), dict) or not isinstance(
        previous.get("outputs", {}), dict
    ):
        raise ValueError("Observation cursor maps are malformed")
    positions = dict(previous.get("positions", {}))
    outputs = dict(previous.get("outputs", {}))
    if any(
        key not in handles or not isinstance(value, str) for key, value in positions.items()
    ) or any(key not in handles for key in outputs):
        raise ValueError("Observation cursor contains an invalid task position")
    for output in outputs.values():
        validate_output_cursor(output)
    rows: list[dict[str, Any]] = []
    settled, any_settled, has_more = True, False, False
    # A page is one result in the reading model's context (at least a character, so a
    # cursor always advances); the transcript preview bound is not the model's.
    bound = max(1, result_room().chars)
    budget: int | None = bound
    start = previous.get("scan_start", 0)
    if type(start) is not int or not 0 <= start < len(handles):
        raise ValueError("Observation cursor has an invalid scan position")
    next_start, first_omitted = start, None
    for scanned in range(len(handles)):
        index = (start + scanned) % len(handles)
        handle = handles[index]
        with history.serialized():
            access = TaskAccess(history, caller)
            try:
                current = access.require(handle, "observe")
            except TaskAccessDenied:
                current = None
            readable = current is not None and "read" in access.rights(current)
            view = access.view(current) if current is not None else {}
        if current is None:
            if positions.get(handle) != "unavailable":
                if row_limit is None or len(rows) < row_limit:
                    rows.append({"handle": handle, "error": "task_unavailable"})
                    positions[handle] = "unavailable"
                else:
                    has_more = True
                    if first_omitted is None:
                        first_omitted = index
            any_settled = True
            continue
        settled = settled and current.terminal_at is not None
        any_settled = (
            any_settled
            or current.terminal_at is not None
            or current.status == "task_state_uncertain"
        )
        position = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()
        output_state = outputs.get(handle, {})
        output_changed = readable and (
            bool(output_state.get("pending"))
            or output_state.get("completed")
            != (current.progress.output.content_id if current.progress.output else None)
        )
        if positions.get(handle) == position and not output_changed:
            continue
        if (row_limit is not None and len(rows) >= row_limit) or (output_changed and budget == 0):
            has_more = True
            if first_omitted is None:
                first_omitted = index
            continue
        # References and metadata are immutable; content I/O does not own the writer lock.
        if readable:
            if include_state:
                view.update(workflow_state(history, current))
            chunks, continuation, used, more = output_page(
                history, current, output_state, budget, pattern
            )
            outputs[handle] = continuation
            if budget is not None:
                budget -= used
            if chunks:
                view.update(output=chunks, output_order="newest_chunks_first")
            view["output_truncated"] = more
            has_more = has_more or more
        positions[handle] = position
        next_start = (index + 1) % len(handles)
        rows.append(view)
    # Recheck the entire batch: reading a later member may overlap a grant change.
    with history.serialized():
        access = TaskAccess(history, caller)
        for row in rows:
            handle = row["handle"]
            if row.get("error"):
                continue
            try:
                latest = access.require(handle, "observe")
            except TaskAccessDenied:
                row.clear()
                row.update(handle=handle, error="task_unavailable")
                positions[handle] = "unavailable"
                outputs.pop(handle, None)
                any_settled = True
            else:
                if "read" not in access.rights(latest):
                    row.clear()
                    row.update(access.view(latest))
                    positions[handle] = hashlib.sha256(
                        json.dumps(row, sort_keys=True).encode()
                    ).hexdigest()
                    outputs.pop(handle, None)
    return {
        "tasks": rows,
        "settled": settled,
        "any_settled": any_settled,
        "timed_out": False,
        "has_more": has_more,
        "cursor": encode_cursor(
            {
                "version": 2,
                "recipient": caller.session_id,
                "handles": list(handles),
                "positions": positions,
                "outputs": outputs,
                "scan_start": first_omitted if first_omitted is not None else next_start,
            }
        ),
    }


def pattern_text(row: dict[str, Any]) -> str:
    """Search only disclosed output and progress messages, never labels or owner metadata."""
    message = row.get("progress", {}).get("message", "")
    parts = [message] if isinstance(message, str) else []
    for chunk in row.get("output", []):
        output = chunk.get("output")
        if output is not None:
            parts.append(
                output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            )
    return "\n".join(parts)
