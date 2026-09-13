"""Observer-only presentation facts captured around built-in tool calls."""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clio_agent.tools.file_policy import validate_read_path, validate_write_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileWriteSnapshot:
    """Pre-write text needed to render a truthful file-change diff."""

    path: str
    content: str


def capture_tool_presentation(name: str, args: dict[str, Any]) -> FileWriteSnapshot | None:
    """Capture pre-call facts without changing or interpreting tool execution."""

    if name != "fs_apply_edit_write":
        return None
    filepath = args.get("filepath")
    if not isinstance(filepath, str) or not filepath:
        return None
    try:
        path = Path(validate_write_path(filepath, field="filepath"))
        if not path.exists():
            return FileWriteSnapshot(path=str(path), content="")
        readable = Path(validate_read_path(str(path), field="filepath"))
        return FileWriteSnapshot(
            path=str(readable), content=readable.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, ValueError) as exc:
        logger.warning(
            "tool presentation snapshot unavailable reason=file_diff_snapshot_failed "
            "tool=%s error=%s",
            name,
            exc,
        )
        return None


def enrich_tool_observer_result(
    result: Any,
    args: dict[str, Any],
    snapshot: FileWriteSnapshot | None,
) -> Any:
    """Add a write diff to the observer projection, never the model observation."""

    if snapshot is None or not isinstance(result, dict):
        return result
    structured = result.get("structuredContent")
    new_content = args.get("new_content")
    if not isinstance(structured, dict) or not isinstance(new_content, str):
        return result
    if structured.get("ok") is not True:
        return result
    diff_lines = list(
        difflib.unified_diff(
            snapshot.content.splitlines(),
            new_content.splitlines(),
            fromfile=f"a/{Path(snapshot.path).name}",
            tofile=f"b/{Path(snapshot.path).name}",
            lineterm="",
        )
    )
    enriched = dict(structured)
    enriched["unified_diff"] = "\n".join(diff_lines)
    enriched["lines_added"] = sum(
        line.startswith("+") and not line.startswith("+++") for line in diff_lines
    )
    enriched["lines_removed"] = sum(
        line.startswith("-") and not line.startswith("---") for line in diff_lines
    )
    return {**result, "structuredContent": enriched}


__all__ = ["FileWriteSnapshot", "capture_tool_presentation", "enrich_tool_observer_result"]
