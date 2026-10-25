"""Human-readable wait receipts, separate from async task execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_UNSUCCESSFUL = {"failed", "error", "cancelled", "interrupted", "denied"}


def is_wait_task_tool(name: str) -> bool:
    """Whether ``name`` is the native wait whose result is a task snapshot."""
    return name == "wait_tasks"


def mark_wait_interrupted(presentation: dict[str, Any], source: str = "") -> None:
    """Mark a returned wait snapshot interrupted without turning it into an error."""
    reason = {
        "mail_received": "Interrupted by mail; pending tasks continue running.",
        "stopped": "Interrupted by a stop request; pending tasks continue running.",
        "session_cancelled": "Interrupted by session cancellation; pending tasks may continue.",
    }.get(source, "Interrupted; pending tasks may continue running.")
    presentation["diagnostic"] = reason
    # Row-level child failures remain in their own error blocks and counts; the
    # wait call itself returned an interrupted snapshot, not a failed invocation.
    presentation["status"] = "interrupted"


def wait_start_presentation(args: Mapping[str, Any]) -> dict[str, Any]:
    """Describe an active wait using authorized task kinds, before a result exists."""
    from clio_agent.gact.context import active_app, active_session_id
    from clio_agent.gact.task_access import TaskAccess

    if args.get("timeout_s") == 0:
        return {"summary": "Checking task status", "blocks": []}
    handles = args.get("tasks", [])
    if isinstance(handles, str):
        handles = [handles]
    app, sid = active_app(), active_session_id()
    if app is None or not sid or not isinstance(handles, list):
        return {"summary": "Waiting for tasks", "blocks": []}
    kinds = []
    for handle in handles:
        try:
            row = TaskAccess(app.state.task_history, app.state.conversations.caller(sid)).require(
                handle, "observe"
            )
        except ValueError:
            return {"summary": "Waiting for tasks", "blocks": []}
        kinds.append(row.kind)
    return {
        "summary": "Waiting for your answer"
        if kinds and set(kinds) == {"Question"}
        else "Waiting for tasks",
        "blocks": [],
    }


def _result_text(value: Any) -> str:
    """Use returned text when available; retain structured results otherwise."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("output"), str):
        return "\n".join(filter(None, (value["output"], str(value.get("error_reason") or ""))))
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def wait_presentation(args: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Show the wait policy, selected handles and each recorded task outcome."""
    mode = "any" if args.get("return_when") == "any" else "all"
    timeout = args.get("timeout_s")
    poll = timeout == 0
    requested = args.get("tasks", [])
    if isinstance(requested, str):
        requested = [requested]
    pending = [handle for handle in result.get("pending", []) if isinstance(handle, str)]
    outcomes = result.get("outcomes", result.get("results", []))
    # Decode released receipt fields without consulting a second task authority.
    outcomes = [
        {**row, "handle": row.get("handle") or row.get("task_id")}
        for row in outcomes
        if isinstance(row, Mapping)
    ]
    rows = [row for row in outcomes if not row.get("error")]
    errors = [row for row in outcomes if row.get("error")]
    errors.extend(row for row in result.get("errors", []) if isinstance(row, Mapping))

    def uncollected(row: Mapping[str, Any]) -> bool:
        return (
            row.get("status") == "task_state_uncertain"
            or bool(row.get("pending"))
            or row.get("handle") in pending
        )

    handles = list(
        dict.fromkeys(
            [handle for handle in requested if isinstance(handle, str)]
            + pending
            + [str(row["handle"]) for row in [*rows, *errors] if row.get("handle")]
        )
    )
    action = f"{'Check' if poll else 'Wait for'} {mode} {'task' if mode == 'any' else 'tasks'}"
    policy = (
        "Return when any requested task settles."
        if mode == "any"
        else "Return when all requested tasks settle."
    )
    if poll:
        policy = "Immediate status check; this call did not wait."
    if result.get("timed_out") and not poll:
        policy += " The deadline expired; pending tasks continue running."
    elif result.get("interrupted") == "mail_received":
        policy += " New input is available to this agent; pending tasks continue running."
    elif result.get("interrupted") == "stopped":
        policy += " This wait stopped; pending tasks continue running."
    elif pending:
        policy += " Pending tasks continue running."
    blocks: list[dict[str, Any]] = [
        {"id": "wait-targets", "type": "text", "text": ", ".join(handles)},
        {"id": "wait-policy", "type": "text", "text": policy},
    ]
    for index, handle in enumerate(handles):
        error = next((row for row in errors if row.get("handle") == handle), None)
        row = next((row for row in rows if row.get("handle") == handle), None)
        block: dict[str, Any] = {"id": f"wait-task-{index}", "type": "text"}
        if error is not None:
            block.update(
                label=f"{handle}: Could not collect",
                text=str(error.get("error") or error.get("message") or "Unknown error"),
                severity="error",
            )
        elif row is not None:
            status = str(row.get("status") or "")
            label = (
                "Assignment complete"
                if row.get("kind") in {"Session", "Subagent"} and status == "completed"
                else status.capitalize() or "Result collected"
            )
            value = row.get("result", row.get("output"))
            block.update(
                label=f"{handle}: {label}",
                text="No result body was recorded." if value is None else _result_text(value),
                status=status,
                result_kind="snapshot" if uncollected(row) else "completion",
            )
            state = row.get("workflow_state")
            if isinstance(state, Mapping):
                detail: list[str] = []
                for group, fields in state.items():
                    if isinstance(fields, Mapping):
                        detail.extend(
                            f"{str(group).replace('_', ' ').capitalize()} {key}: {_result_text(value)}"
                            for key, value in fields.items()
                        )
                    else:
                        detail.append(f"{group}: {_result_text(fields)}")
                block["detail"] = "\n".join(detail)
            if status == "task_state_uncertain":
                block.update(
                    label=f"{handle}: Needs attention",
                    text=str(
                        row.get("reason")
                        or "Execution state could not be confirmed. Inspect this task and its effects before retrying."
                    ),
                    result_kind="snapshot",
                    severity="warning",
                )
            if status in _UNSUCCESSFUL:
                block["severity"] = "error"
        elif handle in pending:
            block.update(
                label=handle,
                text="Pending when this call returned.",
                result_kind="snapshot",
                status="pending",
            )
        else:
            block.update(label=handle, text="No outcome was recorded for this task.")
        blocks.append(block)
    # A malformed/unscoped error still belongs in the visible receipt.
    for index, error in enumerate(errors):
        if not error.get("handle"):
            blocks.append(
                {
                    "id": f"wait-error-{index}",
                    "type": "text",
                    "severity": "error",
                    "text": str(error.get("error") or error.get("message") or error),
                }
            )
    failed = sum(row.get("status") in _UNSUCCESSFUL for row in rows)
    collected = sum(not uncollected(row) for row in rows)
    attention = sum(row.get("status") == "task_state_uncertain" for row in rows)
    counts = [f"{collected} {'result' if collected == 1 else 'results'} collected"]
    if result.get("interrupted") == "mail_received":
        counts.insert(0, "New input received")
    if pending:
        counts.insert(0, f"{len(pending)} pending")
    if failed:
        counts.append(f"{failed} unsuccessful")
    if attention:
        counts.append(f"{attention} {'task needs' if attention == 1 else 'tasks need'} attention")
    if errors:
        counts.append(f"{len(errors)} {'error' if len(errors) == 1 else 'errors'}")
    presentation: dict[str, Any] = {
        "action": action,
        "subject": "wait-targets",
        "summary": ", ".join(counts),
        "blocks": blocks,
    }
    if failed or errors or attention:
        presentation["status"] = "degraded"
    interruption = result.get("interrupted")
    if isinstance(interruption, str) and interruption:
        mark_wait_interrupted(presentation, interruption)
    return presentation
