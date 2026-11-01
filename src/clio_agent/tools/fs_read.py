"""Ranged, line-numbered file reads (the ``fs_read_file`` tool's body).

A read returns a header line and the requested lines, each prefixed with its 1-based
number, as text content, plus a small structured result for programs (``run_code``
scripts, the UI): path, the line range, the file's line count and the next offset.

A read sizes itself to the model's result bound (the smaller of the model and transcript
lanes, as the shell tool does): it returns the whole lines that fit, and when lines
remain its header names the offset to continue from, so a default read never spills
into a file that only this tool could page. Very long lines are cut with a marker.
Binary files are refused with a typed error naming the tool that can read them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from clio_agent.tools.file_policy import validate_non_empty_string, validate_read_path
from clio_agent.tools.mcp_result_projection import (
    model_tool_result_chars,
    transcript_tool_result_chars,
)

__all__ = ["DEFAULT_LIMIT", "MAX_LINE_CHARS", "read_text_range"]

#: Lines a read returns at most when the bound allows (an upper bound, not a target).
DEFAULT_LIMIT = 2000
#: A longer line is cut, with a marker giving its full length.
MAX_LINE_CHARS = 2000
#: Above this size the read stops counting lines once its range is filled.
_COUNT_ALL_BYTES = 64 * 1024 * 1024
_SNIFF_BYTES = 8192
_HEADER_RESERVE = 400

_READERS = {
    **dict.fromkeys(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"), "view_image"
    ),
    ".pdf": "view_pdf",
}


def _binary_error(path: Path) -> ToolError:
    reader = _READERS.get(path.suffix.lower())
    hint = (
        f"read it with {reader}"
        if reader
        else "use a tool or skill for its format, or a script that parses it"
    )
    return ToolError(f"fs_read_binary_file: {path} is not a text file; {hint}.")


def _numbered(number: int, line: str) -> str:
    text = line.rstrip("\n").rstrip("\r")
    if len(text) > MAX_LINE_CHARS:
        text = f"{text[:MAX_LINE_CHARS]}… [line cut: {len(text):,} characters]"
    return f"{number:>6}\t{text}"


def read_text_range(filepath: str, offset: int = 1, limit: int = DEFAULT_LIMIT) -> ToolResult:
    """Read up to ``limit`` lines from line ``offset`` (1-based), sized to the result bound."""

    validate_non_empty_string(filepath, field="filepath")
    path = Path(validate_read_path(filepath))
    if not path.is_file():
        raise ToolError(f"fs_read_not_a_file: {path} is not a file.")
    with path.open("rb") as handle:
        if b"\x00" in handle.read(_SNIFF_BYTES):
            raise _binary_error(path)
    size = path.stat().st_size
    offset, limit = max(1, int(offset)), max(1, int(limit))
    budget = min(model_tool_result_chars(), transcript_tool_result_chars()) - _HEADER_RESERVE
    lines: list[str] = []
    used, end, total, full = 0, offset - 1, 0, False
    counted_all = True
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for number, line in enumerate(handle, start=1):
            total = number
            if number < offset:
                continue
            if not full:
                row = _numbered(number, line)
                if len(lines) < limit and (used + len(row) + 1 <= budget or not lines):
                    lines.append(row)
                    used += len(row) + 1
                    end = number
                    continue
                full = True
            if size > _COUNT_ALL_BYTES:
                counted_all = False
                break
    more = total > end if counted_all else True
    header = _header(path, size, offset, end, total if counted_all else None, more)
    structured: dict[str, Any] = {
        "path": str(path),
        "size_bytes": size,
        "start_line": offset if lines else None,
        "end_line": end if lines else None,
        "total_lines": total if counted_all else None,
        "truncated": more,
        "next_offset": end + 1 if more else None,
    }
    text = "\n".join([header, *lines])
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=structured)


def _header(path: Path, size: int, offset: int, end: int, total: int | None, more: bool) -> str:
    count = f"{total:,}" if total is not None else "an uncounted number of"
    if total == 0:
        return f"{path}  empty file ({size:,} bytes)"
    if end < offset:
        return f"{path}  offset {offset:,} is past the end; the file has {count} lines"
    head = f"{path}  lines {offset:,}-{end:,} of {count}  ({size:,} bytes)"
    return f"{head}; continue with offset={end + 1}" if more else head
