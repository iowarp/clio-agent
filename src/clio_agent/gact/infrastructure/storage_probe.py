"""Dependency-free host filesystem inspection, also executed over the SSH transport.

This module must remain standalone: remote hosts need only the Python standard
library. Requests arrive as JSON on stdin, never interpolated into a shell.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def inspect_path(path: str, *, browse: bool = False, limit: int = 200) -> dict[str, Any]:
    """Inspect a host-native path without creating it or following child symlinks."""
    requested = Path(path)
    if not requested.is_absolute() or any(ord(char) < 32 for char in path):
        raise ValueError("Choose an absolute path on the selected host")
    resolved = requested.resolve()
    ancestor = resolved
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise ValueError("The path or an existing ancestor is not a directory")
    usage = shutil.disk_usage(ancestor)
    entries: list[dict[str, str]] = []
    truncated = False
    if browse:
        if not resolved.is_dir():
            raise ValueError("The folder does not exist on the selected host")
        with os.scandir(resolved) as iterator:
            for item in iterator:
                if item.is_dir(follow_symlinks=False):
                    if len(entries) >= limit:
                        truncated = True
                        break
                    entries.append({"name": item.name, "path": str(Path(item.path))})
        entries.sort(key=lambda item: item["name"].casefold())
    return {
        "path": str(resolved),
        "existing_ancestor": str(ancestor),
        "exists": resolved.is_dir(),
        "writable": os.access(ancestor, os.W_OK | os.X_OK),
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "parent": str(resolved.parent),
        "entries": entries,
        "truncated": truncated,
    }


def main() -> None:
    """Run a bounded inspection requested by the infrastructure control plane."""
    try:
        request = json.load(sys.stdin)
        result = inspect_path(request["path"], browse=bool(request.get("browse")))
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
