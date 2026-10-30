"""The shared read boundary for file previews and the search assistant."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

from fastapi import FastAPI, HTTPException

from clio_agent.gact.routes.workspace_file_listing import resolve_managed_input
from clio_agent.gact.routes.workspace_file_policy import workspace_read_redaction_reason
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime import trace
from clio_agent.tools.file_policy import FileAccessPolicy


def readable_workspace_file(
    app: FastAPI, workspace_id: str, path: str, *, whole_file: bool = True
) -> Path:
    """Resolve an existing file with the same ownership, redaction and size policy as previews.

    ``whole_file=False`` is for callers that read a bounded piece of the file (the HDF5
    viewer's slices); they skip the whole-file size cap and bound each response instead.
    """

    def refuse(status: int, code: str, message: str) -> NoReturn:
        raise HTTPException(
            status,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error=code,
                    message=message,
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    workspace = app.state.workspaces.get(workspace_id)
    if workspace is None:
        refuse(404, "not_found", f"workspace not found: {workspace_id}")
    root = Path(workspace.root_path or os.getcwd()).expanduser().resolve()
    from clio_agent.runtime.workspace_folders import resolve_folder_location

    try:
        additional = resolve_folder_location(root, path)
        managed = additional or resolve_managed_input(app, workspace_id, root, path)
        target = managed or (root / path).resolve()
    except PermissionError as exc:
        refuse(403, "folder_access_denied", str(exc))
    except (OSError, ValueError, RuntimeError):
        refuse(400, "invalid_path", f"could not resolve path: {path}")
    try:
        relative = Path(path) if managed else target.relative_to(root)
    except ValueError:
        refuse(403, "path_outside_workspace", f"path escapes workspace: {path}")
    reason = workspace_read_redaction_reason(relative)
    if reason is not None:
        raise HTTPException(
            403,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="redacted",
                    message=f"refusing to serve raw bytes for {path}: {reason}",
                    recoverable=False,
                    details={"reason": reason},
                )
            ).model_dump(exclude_none=True),
        )
    if not target.is_file():
        refuse(404, "not_found", f"file not found: {path}")
    if additional:
        policy = FileAccessPolicy.from_mapping(os.environ)
        policy._check_connected_storage(target, field="path")
        from clio_agent.tools.file_policy import _has_symlink

        if not policy.allow_symlinks and _has_symlink(target):
            refuse(403, "symlink_denied", "Symlink files are excluded")
    if not whole_file:
        return target
    try:
        max_bytes = FileAccessPolicy.from_mapping(os.environ).max_file_size_bytes
    except (ValueError, TypeError, OSError) as exc:
        trace.event(
            "WORKSPACE", "file policy unavailable reading %s (%s); using 1 GiB size cap", path, exc
        )
        max_bytes = 1024**3
    size = target.stat().st_size
    if size > max_bytes:
        refuse(413, "file_too_large", f"file exceeds policy cap ({size} > {max_bytes} bytes)")
    return target
