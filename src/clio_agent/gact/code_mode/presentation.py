"""How a ``run_code`` call reads: the script, what it printed, its value and its calls.

The row's action is "Run code"; its summary counts the script's tool calls ("14
calls, 1 failed"); the script is a Python block, the output a text block and an error
a stated failure. Nested calls appear as their own rows under this one
(``parent_call_id``), so the result does not repeat them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["present", "starting"]

_STATUS = {"completed": "", "failed": "failed", "timed_out": "failed", "cancelled": "interrupted"}


def _script(code: str) -> dict[str, Any]:
    return {"id": "script", "type": "code", "language": "python", "label": "Script", "text": code}


def starting(args: Mapping[str, Any]) -> dict[str, Any]:
    """Before the script runs (or while it waits for approval): the script itself."""
    code = str(args.get("code") or "")
    return {"subject": "script", "summary": "", "blocks": [_script(code)] if code else []}


def _summary(row: Mapping[str, Any]) -> str:
    calls, failed = int(row.get("calls") or 0), int(row.get("failed_calls") or 0)
    text = f"{calls} call{'s' if calls != 1 else ''}" + (f", {failed} failed" if failed else "")
    status = str(row.get("status") or "")
    if status == "timed_out":
        return f"Stopped at its time limit; {text}"
    if status == "failed":
        return f"Script failed; {text}"
    return text if calls else "No tool calls"


def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """The finished script."""
    row = structured if isinstance(structured, Mapping) else {}
    code = str(row.get("code") or args.get("code") or "")
    blocks: list[dict[str, Any]] = [_script(code)] if code else []
    if not row:
        blocks.append({"id": "result", "type": "text", "text": str(result or "")})
        return {"subject": "script", "summary": "", "blocks": blocks}
    if args.get("background"):
        # A background launch: the script runs on as a task with its own row.
        name = (
            str(args.get("label") or "").strip()
            or next((line.strip() for line in code.splitlines() if line.strip()), "")[:80]
        )
        handle = str(row.get("handle") or "")
        blocks.append(
            {
                "id": "task",
                "type": "text",
                "text": f"Runs in the background as task {handle}; its progress and a "
                "cancel control are in Work, under Background tasks.",
            }
        )
        summary = f"Started in the background: {name}" if name else "Started in the background"
        return {"subject": "script", "summary": summary, "blocks": blocks}
    if row.get("output"):
        blocks.append({"id": "output", "type": "text", "label": "Output", "text": row["output"]})
    if row.get("value") is not None:
        blocks.append({"id": "value", "type": "text", "label": "Value", "text": str(row["value"])})
    if row.get("error"):
        blocks.append(
            {
                "id": "error",
                "type": "text",
                "severity": "error",
                "label": "Error",
                "text": row["error"],
            }
        )
    if not (row.get("output") or row.get("error") or row.get("value") is not None):
        blocks.append({"id": "silent", "type": "text", "text": "The script printed nothing."})
    # The calls themselves are nested rows under this one; naming them again here
    # would repeat them as producer identifiers.
    view: dict[str, Any] = {"subject": "script", "summary": _summary(row), "blocks": blocks}
    status = _STATUS.get(str(row.get("status") or ""), "")
    return view | ({"status": status} if status else {})
