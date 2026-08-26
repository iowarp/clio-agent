"""Persisted workspace-root grant revocation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def revoke_root_grant(
    app: "FastAPI",
    workspace_id: str,
    path: str,
    *,
    grantor: str = "user",
) -> dict[str, Any]:
    """Revoke one persisted additional workspace root and its live projection."""

    from clio_agent.gact.runtime import grants  # noqa: PLC0415
    from clio_agent.runtime.sandbox_roots import revoke_write_root_grant  # noqa: PLC0415

    ws = app.state.workspaces.get(workspace_id)
    if ws is None:
        return {"revoked": False, "pattern": path, "reason": "workspace_not_found"}
    workspace_root = str(getattr(ws, "root_path", "") or "")
    pattern = str(revoke_write_root_grant(workspace_root, path))
    existing = [str(item) for item in ws.config.get(grants.GRANTED_ROOTS_CONFIG_KEY, []) or []]
    remaining = [item for item in existing if item != pattern]
    changed = len(remaining) != len(existing)
    if changed:
        ws.config[grants.GRANTED_ROOTS_CONFIG_KEY] = remaining
        app.state.workspaces.update(workspace_id, metadata_patch=None)
        restart = grants._request_fleet_restart(app, workspace_root, widened=True)
        grants.emit_boundary_revoked(
            app,
            kind=grants.KIND_ROOT,
            scope=grants.SCOPE_WORKSPACE,
            grantor=grantor,
            pattern=pattern,
            workspace_id=workspace_id,
        )
    else:
        restart = "not_granted"
    return {
        "revoked": changed,
        "pattern": pattern,
        "reason": restart,
        "restart_deferred": restart == "restart_deferred_busy",
    }
