"""Filesystem MCP server for CLIO.

Five curated tools. Three read, without executing anything; two write:

    - read_file: pull a file's contents (capped at 256 KB).
    - list_directory: discover an approved folder's immediate entries (bounded).
    - search_text: find literal text with line-numbered context (bounded).
    - write_file: create a file or replace its whole content (Claude Code's Write).
    - edit_file: replace an exact string in an existing file (Claude Code's Edit).

Every tool honors file_policy + workspace boundaries. Reads inside the policy are
always allowed. The two writes change the file on disk; their declared annotations
make them writes, so the permission gate asks or allows by the session's mode and
PreToolUse hooks run on them. GACT records each successful write as an applied
``file_diff`` the person can undo (``POST /v1/sessions/{sid}/diffs/revert``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from clio_agent import conf
from clio_agent.tools import fs_edit
from clio_agent.tools.builtin_annotations import (
    _FILE_WRITE_ANNOTATIONS,
    _READ_FILE_ANNOTATIONS,
)
from clio_agent.tools.builtin_annotations import (
    FS_TOOL_ANNOTATIONS as FS_TOOL_ANNOTATIONS,
)
from clio_agent.tools.file_policy import (
    validate_non_empty_string,
    validate_read_path,
)
from clio_agent.tools.fs_inspection import list_directory as _list_directory
from clio_agent.tools.fs_inspection import search_text as _search_text

fs_server = FastMCP("fs")

# 256 KB cap on direct reads — resolved file → env → default (clio_agent.conf).
_MAX_READ_BYTES = conf.resolve(
    "limits.fs_read_bytes", env="CLIO_FS_MAX_READ_BYTES", default=256 * 1024, cast=conf.as_int
)

# Declared MCP ToolAnnotations — the SINGLE source of truth for each built-in
# tool's effect class (#1061). The catalog read/write tags are now a PROJECTION
# of these hints (``clio_agent.tools.catalog.classification_tags``); no
# hand-authored read/write tag competes with the declaration below.
#
#   - read_file / list_directory / search_text: provably read-only, closed-world
#     (local disk read only).
#   - write_file / edit_file: NOT read-only; a bounded, closed-world destructive
#     write (a single known path), so they project to the catalog ``write`` tag
#     the auto-edits approval mode keys on.


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
def list_directory(path: str, limit: Annotated[int, Field(ge=1, le=1_000)] = 200) -> dict[str, Any]:
    """List immediate entries in an absolute approved directory; does not follow links.

    Use this to discover repository paths without guessing filenames or running shell.
    Results explicitly report truncation at the requested entry limit (maximum 1,000).
    """
    return _list_directory(path, limit)


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
def search_text(
    path: str,
    query: Annotated[str, Field(min_length=1, max_length=1_000)],
    file_glob: str = "*",
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
    context_lines: Annotated[int, Field(ge=0, le=50)] = 2,
    case_sensitive: bool = True,
) -> dict[str, Any]:
    """Search literal text in an approved file or directory, with line-numbered context.

    Use file_glob to narrow source files. Does not follow links, execute code or write.
    Scan/result limits and incomplete files are explicit; no matches is not proof of absence
    when truncated. Binary files and common dependency/Git directories are skipped.
    context_lines supplies 0–50 lines on each side; returned text is capped at 32,000 characters.
    """
    return _search_text(path, query, file_glob, limit, context_lines, case_sensitive)


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
def read_file(
    filepath: str,
    access_reason: Annotated[
        str,
        Field(
            description="Explain why this file is needed when requesting access outside permitted folders."
        ),
    ] = "",
) -> dict[str, Any]:
    """Read a file's contents from disk.

    Capped at 256 KB. Larger files return ``truncated: true`` plus
    the head; the agent should fall back to a format-specific reader
    for big files when one is available.

    Path validated through file_policy — only files inside the
    configured allowed roots are readable.
    """

    validate_non_empty_string(filepath, field="filepath")
    safe = validate_read_path(filepath)
    p = Path(safe)
    raw = p.read_bytes()
    truncated = len(raw) > _MAX_READ_BYTES
    if truncated:
        raw = raw[:_MAX_READ_BYTES]
    return {
        "path": str(p),
        "size_bytes": p.stat().st_size,
        "content": raw.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


@fs_server.tool(annotations=_FILE_WRITE_ANNOTATIONS)
def write_file(
    filepath: str,
    content: str,
    access_reason: Annotated[
        str, Field(description="Explain why this write needs access outside permitted folders.")
    ] = "",
) -> dict[str, Any]:
    """Create a file, or replace its whole content, with ``content``.

    Missing parent folders are created. The bytes on disk are exactly ``content``.
    The result says whether the file was ``created`` and carries the ``unified_diff``
    of the change, its line counts, and the sha256 of the file before and after.
    The change is listed in the person's diff panel, where they can undo it.
    """

    return fs_edit.write_file(filepath, content)


@fs_server.tool(annotations=_FILE_WRITE_ANNOTATIONS)
def edit_file(
    filepath: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    access_reason: Annotated[
        str, Field(description="Explain why this edit needs access outside permitted folders.")
    ] = "",
) -> dict[str, Any]:
    """Replace ``old_string`` with ``new_string`` in an existing text file.

    The match is exact, including whitespace and indentation. ``old_string`` must
    occur exactly once unless ``replace_all`` is true, which replaces every
    occurrence. A missing file, a missing ``old_string``, or one that occurs more
    than once returns a typed error and changes nothing. The result carries the
    ``unified_diff`` of the change, its line counts, and the sha256 before and after.
    """

    return fs_edit.edit_file(filepath, old_string, new_string, replace_all)


__all__ = ["FS_TOOL_ANNOTATIONS", "fs_server"]
