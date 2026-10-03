"""Workspace identity and mutation wire models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator


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
    storage_root: str = Field(default="", description="Deprecated; storage is Agent-managed.")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_legacy_storage(self) -> Self:
        """Accept the old default sent by clients, without creating it or honoring overrides."""
        if self.storage_root:
            if (
                not self.root_path
                or Path(self.storage_root).resolve() != (Path(self.root_path) / ".clio").resolve()
            ):
                raise ValueError("storage_root is Agent-managed; omit this field")
            self.storage_root = ""
        return self


class ListWorkspacesResponse(BaseModel):
    """GET /v1/workspaces body."""

    workspaces: list[Workspace]
