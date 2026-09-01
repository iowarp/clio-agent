"""Workspace identity and mutation wire models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Workspace(BaseModel):
    """A filesystem-root-backed collection of related sessions."""

    id: str
    name: str
    root_path: str = ""
    display_name: str = ""
    path: str = ""
    connection_id: str = "local"
    storage_root: str = ""
    created_at: str
    updated_at: str
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateWorkspaceRequest(BaseModel):
    """POST /v1/workspaces body."""

    name: str
    root_path: str = ""
    storage_root: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListWorkspacesResponse(BaseModel):
    """GET /v1/workspaces body."""

    workspaces: list[Workspace]
