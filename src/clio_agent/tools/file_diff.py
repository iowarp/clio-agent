"""Unified file diffs with exact line separation and explicit EOF markers."""

from __future__ import annotations

import difflib


def unified_file_diff(before: str, after: str, filename: str, *, context: int = 3) -> str:
    """Format a diff without adding blank rows or losing missing final newlines."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=context,
    )
    return "".join(
        line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n" for line in lines
    )
