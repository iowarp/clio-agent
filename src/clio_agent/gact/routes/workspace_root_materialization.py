"""Materialize the filesystem root a workspace pins (leaf helper for the routes).

Both workspace write paths must leave the pinned root usable as a tool cwd:
``POST /v1/workspaces`` creates it, and ``PATCH /v1/workspaces/{wid}`` repoints it.
An accepted-but-absent root is not caught at the route; it surfaces much later as
an ENOENT from the MCP fleet (``MCPSpawnError``) or the shell server
(``cwd_not_found``), on a turn the user did not connect to the workspace edit.

Kept in its own leaf module so :mod:`clio_agent.gact.routes.workspaces` stays under
the file-size ratchet and both handlers share one implementation of the guarantee.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def materialize_workspace_root(root_path: str) -> None:
    """Create ``root_path`` on disk, refusing an unmakeable root with a typed 400.

    Args:
        root_path: The filesystem root the workspace pins. An empty value is a
            no-op -- a workspace may legitimately pin nothing.

    Raises:
        HTTPException: 400 ``invalid_request`` carrying the offending ``root_path``
            and the OS ``reason`` when the directory cannot be created.
    """

    if not root_path:
        return
    try:
        Path(root_path).expanduser().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_request",
                    message=f"workspace root could not be created: {root_path}",
                    details={"root_path": root_path, "reason": str(exc)},
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        ) from exc


__all__ = ["materialize_workspace_root"]
