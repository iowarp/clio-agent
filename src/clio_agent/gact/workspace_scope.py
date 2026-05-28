"""Workspace/global scope helpers for GACT runtime state."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

GLOBAL_WORKSPACE_ID = "ws_global"


@dataclass(frozen=True)
class WorkspaceScope:
    workspace_id: str
    root_path: str
    storage_root: str
    scope: str = "workspace"

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


def default_workspace_storage_root(root_path: str) -> Path:
    """Return the default runtime storage root for one workspace."""

    root = Path(root_path or os.getcwd()).expanduser()
    return root / ".clio"


def resolve_workspace_storage_root(row: Any) -> Path:
    """Return the configured or default storage root for a workspace row."""

    root_path = str(getattr(row, "root_path", "") or os.getcwd())
    config = getattr(row, "config", {}) or {}
    metadata = getattr(row, "metadata", {}) or {}
    configured = ""
    if isinstance(config, Mapping):
        configured = str(config.get("storage_root") or config.get("storage_path") or "")
    if not configured and isinstance(metadata, Mapping):
        configured = str(metadata.get("storage_root") or metadata.get("storage_path") or "")
    if configured:
        return Path(configured).expanduser()
    return default_workspace_storage_root(root_path)


def workspace_scope(row: Any) -> WorkspaceScope:
    """Return normalized runtime scope metadata for one workspace."""

    workspace_id = str(getattr(row, "id", "") or "")
    scope = "global" if workspace_id == GLOBAL_WORKSPACE_ID else "workspace"
    root_path = str(getattr(row, "root_path", "") or "")
    return WorkspaceScope(
        workspace_id=workspace_id,
        root_path=root_path,
        storage_root=str(resolve_workspace_storage_root(row)),
        scope=scope,
    )


def session_scope_label(
    *,
    active_workspace_id: str,
    target_workspace_id: str,
    target_session_id: str = "",
    active_session_id: str = "",
) -> str:
    """Classify a memory/catalog target relative to the active session."""

    if active_session_id and target_session_id == active_session_id:
        return "session"
    if target_workspace_id == GLOBAL_WORKSPACE_ID:
        return "global"
    if active_workspace_id and target_workspace_id == active_workspace_id:
        return "workspace"
    return "other_workspace"
