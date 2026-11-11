"""Bounded, policy-checked directory listing without shell execution.

Text search and glob run on ripgrep (:mod:`clio_agent.tools.fs_search_rg`).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from clio_agent.runtime.sandbox_stubs import is_sandbox_mount_stub
from clio_agent.tools.file_policy import (
    FileAccessPolicy,
    validate_non_empty_string,
    validate_positive_int,
)


def _is_link(entry: os.DirEntry[str] | Path) -> bool:
    attributes = entry.stat(follow_symlinks=False)
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def list_directory(path: str, limit: int = 200) -> dict[str, Any]:
    """List a directory's immediate entries without following links or writing files."""
    validate_non_empty_string(path, field="path")
    validate_positive_int(limit, field="limit", max_value=1_000)
    directory = FileAccessPolicy.from_env().validate_read_directory(path)
    entries: list[dict[str, str]] = []
    truncated = False
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if is_sandbox_mount_stub(Path(entry.path)):
                continue
            if len(entries) == limit:
                truncated = True
                break
            kind = (
                "link"
                if _is_link(entry)
                else ("directory" if entry.is_dir(follow_symlinks=False) else "file")
            )
            entries.append({"name": entry.name, "path": str(Path(entry.path)), "type": kind})
    return {
        "path": str(directory),
        "entries": sorted(entries, key=lambda item: item["name"]),
        "truncated": truncated,
        "limit": limit,
    }
