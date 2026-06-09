"""Filesystem MCP server for CLIO.

Three curated tools that map onto the file_diff workflow GACT
clients already render:

    - read_file: pull a file's contents (capped at 256 KB).
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

import difflib
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from clio_agent.tools.file_policy import (
    validate_non_empty_string,
    validate_read_path,
    validate_write_path,
)
from clio_agent.tools.fs_write import write_text_with_policy

fs_server = FastMCP("fs")

_MAX_READ_BYTES = 256 * 1024  # 256 KB cap on direct reads


@fs_server.tool()
def read_file(filepath: str) -> dict[str, Any]:
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


@fs_server.tool()
def propose_edit(filepath: str, new_content: str) -> dict[str, Any]:
    """Produce a unified diff for an edit WITHOUT touching disk.

    The agent uses this when planning a change: it reads the file,
    computes the new contents, calls propose_edit, and returns the
    diff text. The GACT layer materialises it as a file_diff Part;
    the user approves via /v1/sessions/{sid}/diffs/apply, which
    triggers apply_edit.

    Returns ``{path, unified_diff, new_content, lines_added,
    lines_removed}`` so GACT can later apply the accepted diff without
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
    diff_lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
            lineterm="",
        )
    )
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
    return {
        "path": str(p),
        "unified_diff": "\n".join(diff_lines),
        "new_content": new,
        "lines_added": added,
        "lines_removed": removed,
    }


@fs_server.tool()
def apply_edit_write(filepath: str, new_content: str) -> dict[str, Any]:
    """Write ``new_content`` to ``filepath`` on disk. The tool name
    contains "write" so the destructive-tool permission gate fires
    automatically — direct agent invocation requires user approval.

    Designed for the GACT /diffs/apply path: when the user accepts
    a file_diff, the layer calls apply_edit_write with the full
    new contents. (Not the diff — re-applying a unified diff is
    fragile; we always write the whole file.)
    """

    return write_text_with_policy(filepath, new_content)


__all__ = ["fs_server"]
