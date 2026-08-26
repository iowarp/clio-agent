"""Workspace projections for GACT 0.3."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.protocol.v3 import CONNECTION_ID
from clio_agent.gact.workspaces import workspace_display_name, workspace_path_basename


def workspace_to_v3(workspace: Any) -> dict[str, Any]:
    """Project a workspace without promoting its full path into its label."""

    root_path = str(getattr(workspace, "root_path", "") or "")
    name = str(getattr(workspace, "name", "") or "")
    metadata = getattr(workspace, "metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    workspace_id = str(getattr(workspace, "id", "") or "")
    display_name = workspace_display_name(
        workspace_id=workspace_id,
        name=name,
        root_path=root_path,
        metadata=metadata,
        configured_display_name=str(getattr(workspace, "display_name", "") or ""),
    )
    config = getattr(workspace, "config", {})
    if not isinstance(config, dict):
        config = {}
    granted_roots = [
        str(value)
        for value in config.get("granted_write_roots", []) or []
        if str(value).strip() and str(value) != root_path
    ]
    source_folders = (
        [
            {
                "path": root_path,
                "name": workspace_path_basename(root_path) or root_path,
                "primary": True,
            }
        ]
        if root_path
        else []
    )
    source_folders.extend(
        {
            "path": path,
            "name": workspace_path_basename(path) or path,
            "primary": False,
        }
        for path in dict.fromkeys(granted_roots)
    )
    return {
        "id": workspace_id,
        "name": name or display_name,
        "display_name": display_name,
        "path": root_path,
        "connection_id": str(getattr(workspace, "connection_id", "") or CONNECTION_ID),
        "pinned": bool(metadata.get("pinned", False)),
        "source_folders": source_folders,
    }
