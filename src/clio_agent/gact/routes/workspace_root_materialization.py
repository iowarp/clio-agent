"""Materialize the filesystem root a workspace pins (leaf helper for the routes).

Both workspace write paths must leave the pinned root usable as a tool cwd:
``POST /v1/workspaces`` creates it, and ``PATCH /v1/workspaces/{wid}`` repoints it.
An accepted-but-absent root is not caught at the route; it surfaces much later as
an ENOENT from the MCP fleet (``MCPSpawnError``) or the shell server
(``cwd_not_found``), on a turn the user did not connect to the workspace edit.

Both paths also refuse a root another workspace already pins (gact-tui#219); the
store detects it and :func:`workspace_root_conflict` turns it into the 409 the
client shows.

Kept in its own leaf module so :mod:`clio_agent.gact.routes.workspaces` stays under
the file-size ratchet and both handlers share one implementation of the guarantee.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.gact.workspace_roots import WorkspaceRootConflict
from clio_agent.gact.workspaces import workspace_display_name

#: Unicode categories refused in a root: controls (newline, tab) and invisible
#: format characters (zero-width spaces, bidirectional overrides, the BOM).
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf"})


def _reject_control_characters(root_path: str) -> None:
    """Refuse a root containing a control or format character with a typed 400.

    A newline or tab in a root is almost always client-side escape mangling, and
    POSIX will happily create the mangled directory; an invisible format
    character makes two roots look identical. Every later consumer (MCP fleet
    cwd, artifact writes) then fails with an error that does not mention the
    root (#1193). Same status and error code as the mkdir failure below.
    """

    for index, char in enumerate(root_path):
        if unicodedata.category(char) in _REFUSED_CATEGORIES:
            code_point = f"U+{ord(char):04X}"
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message=(
                            f"workspace root contains control character {code_point} "
                            f"at index {index}"
                        ),
                        details={
                            "root_path": root_path,
                            "character": code_point,
                            "index": index,
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )


def materialize_workspace_root(root_path: str) -> None:
    """Create ``root_path`` on disk, refusing an unusable root with a typed error.

    Args:
        root_path: The filesystem root the workspace pins. An empty value is a
            no-op -- a workspace may legitimately pin nothing.

    Raises:
        HTTPException: 400 ``invalid_request`` naming the code point and index
            when ``root_path`` contains a control or format character, or
            carrying the offending ``root_path`` and the OS ``reason`` when the
            directory cannot be created.
    """

    if not root_path:
        return
    _reject_control_characters(root_path)
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


def workspace_root_conflict(exc: WorkspaceRootConflict) -> HTTPException:
    """The 409 for a root another workspace already pins.

    Args:
        exc: The store's conflict, naming the workspace that owns the folder.

    Returns:
        An ``HTTPException`` whose ``workspace_root_conflict`` envelope names that
        workspace, so a client can offer to open it instead.
    """

    existing = exc.existing
    label = workspace_display_name(
        workspace_id=existing.id,
        name=existing.name,
        root_path=existing.root_path,
        metadata=existing.metadata,
        configured_display_name=existing.display_name,
    )
    return HTTPException(
        status_code=409,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="workspace_root_conflict",
                message=f'{exc.root_path} is already the folder of workspace "{label}"',
                details={
                    "root_path": exc.root_path,
                    "workspace_id": existing.id,
                    "workspace_name": label,
                    "workspace_root": existing.root_path,
                },
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


__all__ = ["materialize_workspace_root", "workspace_root_conflict"]
