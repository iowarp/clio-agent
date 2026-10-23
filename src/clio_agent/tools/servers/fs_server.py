"""Filesystem MCP server for CLIO.

    - read_file: pull a file's contents (capped at 256 KB).
    - glob: list files under a folder matching a glob pattern (bounded).
    - grep: search file contents for a regular expression (bounded).
    - write_file: create a file or replace its whole content (Claude Code's Write).
    - edit_file: replace an exact string in an existing file (Claude Code's Edit).

Every tool honors file_policy + workspace boundaries. Reads inside the policy are
always allowed. The two writes change the file on disk; their declared annotations
make them writes, so the permission gate asks or allows by the session's mode and
PreToolUse hooks run on them. GACT records each successful write as an applied
``file_diff`` the person can undo (``POST /v1/sessions/{sid}/diffs/revert``).
"""

from __future__ import annotations

import fnmatch
import os
import re
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
    FileAccessPolicy,
    FilePolicyError,
    validate_non_empty_string,
    validate_read_path,
    validate_search_root,
)

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
#   - read_file / glob / grep: provably read-only, closed-world (local disk read only).
#   - write_file / edit_file: NOT read-only; a bounded, closed-world destructive
#     write (a single known path), so they project to the catalog ``write`` tag
#     the auto-edits approval mode keys on.


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


#: Folders a search never descends into (version control and dependency trees).
_SKIPPED_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv"})
_MAX_GLOB_RESULTS = 500
_MAX_GREP_MATCHES = 200
_MAX_GREP_FILE_BYTES = 2 * 1024 * 1024


def _walk_files(root: Path) -> Any:
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS)
        for name in sorted(files):
            yield Path(current) / name


def _matches(relative: str, pattern: str) -> bool:
    if "/" not in pattern and not pattern.startswith("**"):
        return fnmatch.fnmatch(Path(relative).name, pattern)
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative, pattern[3:])
    return fnmatch.fnmatch(relative, pattern)


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
def glob(
    pattern: str,
    path: str = ".",
    access_reason: Annotated[
        str,
        Field(
            description="Explain why this folder is needed when it is outside permitted folders."
        ),
    ] = "",
) -> dict[str, Any]:
    """Find files under ``path`` whose path matches a glob ``pattern``.

    ``*.py`` matches file names anywhere below ``path``; ``src/**/*.py`` matches
    relative paths. Version-control and dependency folders are skipped. At most
    500 paths are returned, sorted; ``truncated`` says when more matched.
    """

    validate_non_empty_string(pattern, field="pattern")
    root = Path(validate_search_root(path or ".", field="path"))
    if not root.is_dir():
        raise ValueError(f"path is not a folder: {root}")
    found: list[str] = []
    truncated = False
    for file in _walk_files(root):
        relative = file.relative_to(root).as_posix()
        if _matches(relative, pattern):
            if len(found) == _MAX_GLOB_RESULTS:
                truncated = True
                break
            found.append(str(file))
    return {"root": str(root), "pattern": pattern, "paths": found, "truncated": truncated}


@fs_server.tool(annotations=_READ_FILE_ANNOTATIONS)
def grep(
    pattern: str,
    path: str = ".",
    glob: str = "",
    ignore_case: bool = False,
    access_reason: Annotated[
        str,
        Field(
            description="Explain why this folder is needed when it is outside permitted folders."
        ),
    ] = "",
) -> dict[str, Any]:
    """Search file contents under ``path`` for a regular expression.

    ``path`` may be a file or a folder; ``glob`` limits which files are read
    (for example ``*.md``). Binary files and files over 2 MB are skipped and
    counted. At most 200 matching lines are returned with their line numbers;
    ``truncated`` says when more matched.
    """

    validate_non_empty_string(pattern, field="pattern")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    root = Path(validate_search_root(path or ".", field="path"))
    policy = FileAccessPolicy.from_env()
    files = [root] if root.is_file() else list(_walk_files(root))
    matches: list[dict[str, Any]] = []
    skipped = 0
    truncated = False
    for file in files:
        relative = file.name if root.is_file() else file.relative_to(root).as_posix()
        if glob and not _matches(relative, glob):
            continue
        try:
            if file.stat().st_size > _MAX_GREP_FILE_BYTES:
                skipped += 1
                continue
            data = policy.validate_read(str(file), field="path").read_bytes()
        except (OSError, FilePolicyError):
            skipped += 1
            continue
        if b"\0" in data[:8192]:
            skipped += 1
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                if len(matches) == _MAX_GREP_MATCHES:
                    truncated = True
                    break
                matches.append({"path": str(file), "line": number, "text": line[:500]})
        if truncated:
            break
    return {
        "root": str(root),
        "pattern": pattern,
        "matches": matches,
        "skipped_files": skipped,
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
