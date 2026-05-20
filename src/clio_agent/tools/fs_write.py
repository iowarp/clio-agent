"""Shared filesystem write operations for CLIO edit workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.tools.file_policy import validate_non_empty_string, validate_write_path


def write_text_with_policy(filepath: str, new_content: str) -> dict[str, Any]:
    """Write text after enforcing CLIO's write file policy.

    This is the single implementation behind the MCP ``fs_apply_edit_write``
    tool and the user-approved GACT ``/diffs/apply`` path. Callers may add
    their own permission prompts or audit records before invoking it.
    """

    validate_non_empty_string(filepath, field="filepath")
    safe = validate_write_path(filepath, field="filepath")
    path = Path(safe)
    body = new_content if isinstance(new_content, str) else str(new_content)
    path.write_text(body, encoding="utf-8")
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "ok": True,
    }
