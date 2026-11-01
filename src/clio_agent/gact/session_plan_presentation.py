"""How a ``plan_update`` row reads: what changed in the plan, and its status in words."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PureWindowsPath
from typing import Any

__all__ = ["present"]

_STATUS = {
    "draft": "Draft, in plan mode",
    "approved": "Approved, being worked on",
    "done": "Done",
    "abandoned": "Abandoned",
}


def present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """The plan file, the status, and the written plan or the replaced passage."""
    del result
    row = structured if isinstance(structured, Mapping) else {}
    path = str(row.get("plan_file") or "")
    status = str(row.get("status") or "")
    blocks: list[dict[str, Any]] = []
    if path:
        name = PureWindowsPath(path).name
        blocks.append(
            {"id": "plan-file", "type": "link", "target": "file", "uri": path, "label": name}
        )
    if status:
        blocks.append(
            {"id": "status", "type": "text", "label": "Status", "text": _STATUS.get(status, status)}
        )
    change = str(row.get("change") or "")
    if change == "whole" and args.get("content"):
        blocks.append({"id": "plan", "type": "markdown", "text": str(args["content"])})
        summary = "Plan written"
    elif change == "passage":
        blocks.append(
            {"id": "old", "type": "text", "label": "Replaced", "text": str(row.get("old") or "")}
        )
        blocks.append({"id": "new", "type": "markdown", "text": str(row.get("new") or "")})
        summary = "Plan passage replaced"
    else:
        summary = f"Plan {status}" if status else "Plan unchanged"
    return {"subject": "plan-file" if path else "status", "summary": summary, "blocks": blocks}
