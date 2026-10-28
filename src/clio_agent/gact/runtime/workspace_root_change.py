"""Keep persisted workspace folders and the live sandbox projection aligned."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.workspaces import Workspace


def require_idle_workspace(app: FastAPI, workspace_id: str) -> None:
    """Refuse a primary-folder change while a turn still owns the old folder."""
    runner = app.state.turn_runner
    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    query = PageQuery(workspace_id=workspace_id, archived=None)
    busy = [sid for sid in app.state.sessions.ids(query) if runner.busy(sid)]
    if busy:
        raise HTTPException(
            status_code=409,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="workspace_busy",
                    message="Stop or finish running conversations before changing the primary folder.",
                    details={"workspace_id": workspace_id, "session_ids": busy},
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )


def rebind_workspace_root_grants(app: FastAPI, workspace: Workspace, prior_root: str) -> None:
    """Rebind this workspace's additional folders after its primary path changes.

    The registry and resident fleets are keyed by primary path. Preserve grants
    belonging to another workspace at the old path, and invalidate fleets through
    their existing drain-aware restart owner. Sessions remain keyed by workspace id.
    """
    from clio_agent.gact.runtime import grants  # noqa: PLC0415
    from clio_agent.runtime.sandbox_roots import (  # noqa: PLC0415
        register_write_root_grant,
        revoke_write_root_grant,
    )

    previous = Path(prior_root).expanduser().resolve()
    current = Path(workspace.root_path).expanduser().resolve()
    if current == previous:
        return
    retained = {
        Path(path).expanduser().resolve()
        for row in app.state.workspaces.list()
        if row.id != workspace.id and Path(row.root_path).expanduser().resolve() == previous
        for path in row.config.get(grants.GRANTED_ROOTS_CONFIG_KEY, []) or []
    }
    for path in workspace.config.get(grants.GRANTED_ROOTS_CONFIG_KEY, []) or []:
        resolved = register_write_root_grant(workspace.root_path, path)
        if resolved not in retained:
            revoke_write_root_grant(prior_root, path)
    # Also retire a warmed executor at the old cwd. A fleet for the destination
    # might already exist (another workspace uses it), so refresh its territory.
    grants._request_fleet_restart(app, prior_root, widened=True)
    grants._request_fleet_restart(app, workspace.root_path, widened=True)
