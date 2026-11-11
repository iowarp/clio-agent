"""Results shaped exactly as the fs tools return them, for presenter tests."""

from __future__ import annotations

from typing import Any


def read_result(
    path: str, content: str, *, start: int = 1, total: int | None = None
) -> dict[str, Any]:
    """An ``fs_read_file`` observer result: numbered text plus its structured summary."""
    lines = content.split("\n")
    end = start + len(lines) - 1
    total = end if total is None else total
    numbered = [f"{number:>6}\t{line}" for number, line in enumerate(lines, start)]
    header = f"{path}  lines {start}-{end} of {total}  ({len(content)} bytes)"
    return {
        "content": [{"type": "text", "text": "\n".join([header, *numbered])}],
        "structuredContent": {
            "path": path,
            "size_bytes": len(content),
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            "truncated": end < total,
            "next_offset": end + 1 if end < total else None,
        },
    }
