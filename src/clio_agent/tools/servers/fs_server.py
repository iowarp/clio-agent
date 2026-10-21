"""Filesystem MCP server for CLIO.

Curated tools that map onto the file_diff workflow GACT clients
already render:

    - read_file: pull a file's contents (capped at 256 KB).
    - glob: list files under a folder matching a glob pattern (bounded).
    - grep: search file contents for a regular expression (bounded).
    - propose_edit: produce a unified diff against a candidate
      replacement WITHOUT touching disk. Returns the diff text;
      the GACT layer materialises it as a file_diff Part the user
      approves via /v1/sessions/{sid}/diffs/apply.
    - apply_edit: actually write the edit to disk. Designed to be
      invoked by the GACT layer when /diffs/apply fires, NOT
      directly by the agent (the agent should call propose_edit).
      Path validation goes through file_policy.

All three honor file_policy + workspace boundaries; reads are
always allowed inside the policy, writes go through the
permission gate (apply_edit's name matches "edit"/"write" so the
gate fires).
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
from clio_agent.tools.builtin_annotations import (
    _APPLY_EDIT_WRITE_ANNOTATIONS,
    _PROPOSE_EDIT_ANNOTATIONS,
    _READ_FILE_ANNOTATIONS,
)
from clio_agent.tools.builtin_annotations import (
    FS_TOOL_ANNOTATIONS as FS_TOOL_ANNOTATIONS,
)
from clio_agent.tools.file_diff import unified_file_diff
from clio_agent.tools.file_policy import (
    FileAccessPolicy,
    FilePolicyError,
    validate_non_empty_string,
    validate_read_path,
    validate_search_root,
    validate_write_path,
)
from clio_agent.tools.fs_write import write_text_with_policy

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
#   - read_file: provably read-only, closed-world (local disk read only).
#   - propose_edit: read-only DELIBERATELY — it stages a unified diff WITHOUT
#     touching disk, so the permission gate must treat it as a read (preserving
#     the pre-#1061 "read" classification). readOnlyHint=True carries that truth.
#   - apply_edit_write: NOT read-only; a bounded, closed-world destructive write
#     (a single known path), so it projects to the catalog ``write`` tag the
#     auto-edits approval mode keys on.


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
        Field(description="Explain why this folder is needed when it is outside permitted folders."),
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
        Field(description="Explain why this folder is needed when it is outside permitted folders."),
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


@fs_server.tool(annotations=_PROPOSE_EDIT_ANNOTATIONS)
def propose_edit(
    filepath: str,
    new_content: str,
    access_reason: Annotated[
        str, Field(description="Explain why this file edit needs access outside permitted folders.")
    ] = "",
) -> dict[str, Any]:
    """Produce a unified diff for an edit WITHOUT touching disk.

    The agent uses this when planning a change: it reads the file,
    computes the new contents, calls propose_edit, and returns the
    diff text. The GACT layer materialises it as a file_diff Part;
    the user approves via /v1/sessions/{sid}/diffs/apply, which
    triggers apply_edit.

    Returns the diff and authored content with ``applied: false`` and
    ``requires_review: true``. GACT can later apply the accepted diff without
    trying to replay a patch.
    """

    validate_non_empty_string(filepath, field="filepath")
    safe_write = validate_write_path(filepath, field="filepath")
    p = Path(safe_write)
    if not p.exists():
        # Treat as a new file — diff against empty.
        old = ""
    else:
        safe_read = validate_read_path(str(p), field="filepath")
        p = Path(safe_read)
        old = p.read_text(encoding="utf-8", errors="replace")
    new = new_content if isinstance(new_content, str) else str(new_content)
    diff = unified_file_diff(old, new, p.name)
    diff_lines = diff.splitlines()
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
    return {
        "path": str(p),
        "unified_diff": diff,
        "new_content": new,
        "lines_added": added,
        "lines_removed": removed,
        "applied": False,
        "requires_review": True,
    }


@fs_server.tool(
    annotations=_APPLY_EDIT_WRITE_ANNOTATIONS,
    meta={"ui": {"visibility": ["model:plan"]}},
)
def apply_edit_write(
    filepath: str,
    new_content: str,
    access_reason: Annotated[
        str, Field(description="Explain why this write needs access outside permitted folders.")
    ] = "",
) -> dict[str, Any]:
    """Write ``new_content`` to ``filepath`` on disk. Its declared MCP
    annotations (``destructiveHint=True``, not read-only) project to the
    catalog ``write`` tag. The tool remains in the effective runtime catalog
    for audit and harness execution. It is model-visible only while the active
    session is in Plan mode, where the permission resolver limits it to the
    recorded plan file in Agent-managed workspace state. Outside Plan mode the model proposes a
    reviewable edit and the approved ``/diffs/apply`` route owns this write.

    Designed for the GACT /diffs/apply path: when the user accepts
    a file_diff, the layer calls apply_edit_write with the full
    new contents. (Not the diff — re-applying a unified diff is
    fragile; we always write the whole file.)
    """

    return write_text_with_policy(filepath, new_content)


__all__ = ["FS_TOOL_ANNOTATIONS", "fs_server"]
