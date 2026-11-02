"""Ranged, line-numbered file reads (the ``fs_read_file`` tool's body).

A read returns a header line and the requested lines, each prefixed with its 1-based
number, as text content, plus a small structured result for programs (``run_code``
scripts, the UI): path, the line range, the file's line count and the next offset.

A read sizes itself to the room one result has in the model's context window
(:mod:`clio_agent.tools.result_room`): it returns up to ``limit`` whole lines that fit,
and when lines remain its header names the offset to continue from (and, when the room
stopped it, how big one result may be), so a read never spills into a file that only
this tool could page. Very long lines are cut with a marker.
Binary files are refused with a typed error naming the tool that can read them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from clio_agent import conf
from clio_agent.tools.file_policy import validate_non_empty_string, validate_read_path
from clio_agent.tools.result_room import ResultRoom, result_room

__all__ = ["count_bytes", "read_line_chars", "read_lines", "read_text_range"]

_SNIFF_BYTES = 8192
_HEADER_RESERVE = 400

_READERS = {
    **dict.fromkeys(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"), "view_image"
    ),
    ".pdf": "view_pdf",
}


def _at_least_one(key: str, env: str, default: int) -> int:
    value = conf.resolve(key, env=env, default=default, cast=conf.as_int)
    if value < 1:
        raise ValueError(f"{key} must be at least 1, not {value}")
    return value


def read_lines() -> int:
    """Lines a read returns at most when the bound allows (an upper bound, not a target)."""
    return _at_least_one("fs.read_lines", "CLIO_FS_READ_LINES", 2000)


def read_line_chars() -> int:
    """A longer line is cut, with a marker giving its full length."""
    return _at_least_one("fs.read_line_chars", "CLIO_FS_READ_LINE_CHARS", 2000)


def count_bytes() -> int:
    """Above this file size a read stops counting lines once its range is filled."""
    return _at_least_one("fs.count_bytes", "CLIO_FS_COUNT_BYTES", 64 * 1024 * 1024)


def _binary_error(path: Path) -> ToolError:
    reader = _READERS.get(path.suffix.lower())
    hint = (
        f"read it with {reader}"
        if reader
        else "use a tool or skill for its format, or a script that parses it"
    )
    return ToolError(f"fs_read_binary_file: {path} is not a text file; {hint}.")


def _numbered(number: int, line: str, max_chars: int) -> str:
    text = line.rstrip("\n").rstrip("\r")
    if len(text) > max_chars:
        text = f"{text[:max_chars]}… [line cut: {len(text):,} characters]"
    return f"{number:>6}\t{text}"


def read_text_range(filepath: str, offset: int = 1, limit: int = 0) -> ToolResult:
    """Read up to ``limit`` lines from line ``offset`` (1-based), sized to the result's room.

    ``limit`` 0 reads up to :func:`read_lines` lines.
    """

    validate_non_empty_string(filepath, field="filepath")
    path = Path(validate_read_path(filepath))
    if not path.is_file():
        raise ToolError(f"fs_read_not_a_file: {path} is not a file.")
    with path.open("rb") as handle:
        if b"\x00" in handle.read(_SNIFF_BYTES):
            raise _binary_error(path)
    size = path.stat().st_size
    offset, limit = max(1, int(offset)), max(1, int(limit or read_lines()))
    line_chars, count_limit = read_line_chars(), count_bytes()
    room = result_room()
    budget = room.chars - _HEADER_RESERVE
    lines: list[str] = []
    used, end, total, full = 0, offset - 1, 0, False
    counted_all = True
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for number, line in enumerate(handle, start=1):
            total = number
            if number < offset:
                continue
            if not full:
                row = _numbered(number, line, line_chars)
                if len(lines) < limit and (used + len(row) + 1 <= budget or not lines):
                    lines.append(row)
                    used += len(row) + 1
                    end = number
                    continue
                full = True
            if size > count_limit:
                counted_all = False
                break
    counted = total if counted_all else None
    more = total > end if counted_all else True
    by_room = more and len(lines) < limit
    text = "\n".join([_header(path, size, offset, end, counted, more, room, by_room), *lines])
    # The model's tokenizer can count more than four characters a token: keep what fits.
    while len(lines) > 1 and not room.fits(text):
        keep = len(lines) * room.tokens // max(1, room.measure(text))
        lines = lines[: max(1, min(len(lines) - 1, keep))]
        end, more, by_room = offset + len(lines) - 1, True, True
        text = "\n".join([_header(path, size, offset, end, counted, more, room, by_room), *lines])
    structured: dict[str, Any] = {
        "path": str(path),
        "size_bytes": size,
        "start_line": offset if lines else None,
        "end_line": end if lines else None,
        "total_lines": counted,
        "truncated": more,
        "next_offset": end + 1 if more else None,
    }
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=structured)


def _header(
    path: Path,
    size: int,
    offset: int,
    end: int,
    total: int | None,
    more: bool,
    room: ResultRoom,
    by_room: bool,
) -> str:
    count = f"{total:,}" if total is not None else "an uncounted number of"
    if total == 0:
        return f"{path}  empty file ({size:,} bytes)"
    if end < offset:
        return f"{path}  offset {offset:,} is past the end; the file has {count} lines"
    head = f"{path}  lines {offset:,}-{end:,} of {count}  ({size:,} bytes)"
    if not more:
        return head
    reason = f" (one result holds about {room.tokens:,} tokens)" if by_room else ""
    return f"{head}; continue with offset={end + 1}{reason}"
