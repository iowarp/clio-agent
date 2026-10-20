"""Human-readable wait receipts, separate from async task execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_UNSUCCESSFUL = {"failed", "error", "cancelled", "interrupted", "denied"}


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
    rows = [row for row in result.get("results", []) if isinstance(row, Mapping)]
    errors = [row for row in result.get("errors", []) if isinstance(row, Mapping)]
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
                "Turn ended"
                if row.get("kind") == "Subagent" and status == "completed"
                else status.capitalize() or "Result collected"
            )
            value = row.get("result")
            block.update(
                label=f"{handle}: {label}",
                text="No result body was recorded." if value is None else _result_text(value),
                status=status,
                result_kind="completion",
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
    counts = [f"{len(rows)} {'result' if len(rows) == 1 else 'results'} collected"]
    if pending:
        counts.insert(0, f"{len(pending)} pending")
    if failed:
        counts.append(f"{failed} unsuccessful")
    if errors:
        counts.append(f"{len(errors)} {'error' if len(errors) == 1 else 'errors'}")
    presentation = {
        "action": action,
        "subject": "wait-targets",
        "summary": ", ".join(counts),
        "blocks": blocks,
    }
    if failed or errors:
        presentation["status"] = "degraded"
    return presentation
