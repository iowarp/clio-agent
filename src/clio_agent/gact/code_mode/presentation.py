"""How a ``run_code`` call reads: the script, what it printed, its value and its calls.

The row reads "Run code" and names the script: its label when the agent gave one, else
its first comment, else its first line that is not an import. The summary counts the
script's tool calls ("14 calls, 1 failed"); the script is a Python block (the request,
shown first on expansion), the output a text block and an error a stated failure. Nested calls appear as their own rows under this one
(``parent_call_id``), so the result does not repeat them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["present", "starting"]

_STATUS = {"completed": "", "failed": "failed", "timed_out": "failed", "cancelled": "interrupted"}


def _script(code: str) -> dict[str, Any]:
    return {"id": "script", "type": "code", "language": "python", "label": "Script", "text": code}


def script_name(args: Mapping[str, Any], code: str) -> str:
    """What the row names: the label, else the first comment, else the first statement
    that is not an import (at most 80 characters)."""
    label = str(args.get("label") or "").strip()
    if label:
        return label[:80]
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    comment = next((line.lstrip("#").strip() for line in lines if line.startswith("#")), "")
    if comment:
        return comment[:80]
    statement = next(
        (line for line in lines if not line.startswith(("import ", "from ", "#"))),
        lines[0] if lines else "",
    )
    return statement[:80]


def _view(args: Mapping[str, Any], code: str, summary: str, blocks: list[dict[str, Any]]) -> dict:
    """A view whose subject is the script's name and whose first block is the script."""
    name = script_name(args, code)
    head = [{"id": "target", "type": "text", "label": name, "text": name}] if name else []
    script = [_script(code)] if code else []
    return {
        "subject": "target" if name else "script",
        "summary": summary,
        "blocks": [*head, *script, *blocks],
    }


def starting(args: Mapping[str, Any]) -> dict[str, Any]:
    """Before the script runs (or while it waits for approval): the script itself."""
    return _view(args, str(args.get("code") or ""), "", [])


def _summary(row: Mapping[str, Any]) -> str:
    calls, failed = int(row.get("calls") or 0), int(row.get("failed_calls") or 0)
    text = f"{calls} call{'s' if calls != 1 else ''}" + (f", {failed} failed" if failed else "")
    status = str(row.get("status") or "")
    if status == "timed_out":
        return f"Stopped at its time limit; {text}"
    if status == "failed":
        return f"Script failed; {text}"
    if status == "cancelled":
        return f"Stopped; {text}"
    # A finished script that called no tools still ran: say so, not just "no calls".
    return text if calls else "Finished without tool calls"


def _attached(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The images/PDFs the script viewed, which rode its result to the model."""
    from clio_agent.gact.agents.native_presenters_workspace_file import (  # noqa: PLC0415
        viewed_file_block,
    )

    blocks: list[dict[str, Any]] = []
    for index, item in enumerate(row.get("attached_media") or ()):
        if not isinstance(item, Mapping):
            continue
        seen = {**item, "attached_to_model": True, "delivery": "script_result"}
        if block := viewed_file_block(seen, f"attached-{index + 1}"):
            blocks.append(block)
    return blocks


def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """The finished script."""
    row = structured if isinstance(structured, Mapping) else {}
    code = str(row.get("code") or args.get("code") or "")
    blocks: list[dict[str, Any]] = []
    if not row:
        blocks.append({"id": "result", "type": "text", "text": str(result or "")})
        return _view(args, code, "", blocks)
    if args.get("background"):
        # A background launch: the script runs on as a task with its own row.
        handle = str(row.get("handle") or "")
        blocks.append(
            {
                "id": "task",
                "type": "text",
                "text": f"Runs in the background as task {handle}; its progress and a "
                "cancel control are in Work, under Background tasks.",
            }
        )
        if handle:
            # Opens this task's row under Background tasks (the ``task`` target names a
            # background task by its handle).
            blocks.append(
                {
                    "id": "open-task",
                    "type": "link",
                    "target": "task",
                    "uri": handle,
                    "label": "Show the task in Work",
                }
            )
        return _view(args, code, "Started in the background", blocks)
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
    blocks.extend(_attached(row))
    # The calls themselves are nested rows under this one; naming them again here
    # would repeat them as producer identifiers.
    view = _view(args, code, _summary(row), blocks)
    status = _STATUS.get(str(row.get("status") or ""), "")
    return view | ({"status": status} if status else {})
