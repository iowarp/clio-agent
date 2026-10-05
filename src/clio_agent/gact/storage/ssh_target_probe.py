"""Read-only file operations sent through the existing infrastructure SSH transport.

This module is standalone so the selected host needs only Python's standard library.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def selected_path(root: Path, relative: str) -> Path:
    """Constrain a relative path to the selected folder without following nested links."""
    parts = relative.split("/")
    if relative and any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
        raise ValueError("Invalid source-relative path")
    current = root
    for part in parts if relative else []:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Symbolic links require an explicit source selection")
    current.resolve().relative_to(root)
    return current


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    """List bounded directory pages or read bounded file chunks on the owning host."""
    root = Path(request["root"])
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Choose an existing absolute folder on the selected host")
    root = root.resolve()
    relative = request.get("path", "")
    path = selected_path(root, relative)
    action = request["action"]
    if action == "list":
        offset = int(request.get("offset", 0))
        if offset < 0:
            raise ValueError("Invalid listing offset")
        # Directory metadata only; file hashing is a separate bounded response.
        names = sorted(os.listdir(path))
        if len(names) > 100_000:
            raise ValueError("Select a smaller source folder")
        rows = []
        for name in names[offset : offset + 16]:
            child = selected_path(root, "/".join(filter(None, [relative, name])))
            if not child.is_file() and not child.is_dir():
                raise ValueError("Only regular files and directories can be transferred")
            rows.append({"name": name, "directory": child.is_dir()})
        return {"entries": rows, "next": offset + 16 if offset + 16 < len(names) else None}
    if action == "stat":
        with path.open("rb") as stream:
            revision = hashlib.file_digest(stream, "sha256").hexdigest()
            size = os.fstat(stream.fileno()).st_size
        return {"size": size, "revision": revision}
    if action == "read":
        offset = int(request.get("offset", 0))
        if offset < 0:
            raise ValueError("Invalid read offset")
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(8192)
        return {"data": base64.b64encode(chunk).decode("ascii")}
    raise ValueError("Unsupported source operation")


if __name__ == "__main__":
    try:
        print(json.dumps(dispatch(json.load(sys.stdin))))
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
