"""Validated inputs for trusted connected-source setup and lifecycle operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BrowseSource(BaseModel):
    """Browse a folder or search within the explicitly approved source root."""

    model_config = ConfigDict(extra="forbid")
    folder: str = ""
    query: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)
    materialized: bool = False


class CompleteSignIn(BaseModel):
    """Trusted setup input: never registered as an agent-facing tool schema."""

    model_config = ConfigDict(extra="forbid")
    flow_id: str = Field(min_length=1, max_length=512)
    callback_url: str = Field(default="", max_length=16384, repr=False)


class GitHubRevisions(BaseModel):
    """Bounded, read-only discovery for the repository setup form."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=4096)
    kind: Literal["branch", "tag", "commit"] = "branch"
    page: int = Field(default=1, ge=1, le=1000)


class StartSignIn(BaseModel):
    """Optional Desktop receiver; local browser requests use a CLIO-managed receiver."""

    model_config = ConfigDict(extra="forbid")
    desktop_redirect: str | None = None


class ApplyChanges(BaseModel):
    """Explicit selection from an inspected working-copy review."""

    model_config = ConfigDict(extra="forbid")
    review_id: str
    paths: list[str] = Field(min_length=1, max_length=10000)


class AttachSourceFile(BaseModel):
    """A selected file or folder index from an approved source."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(default="", max_length=4096)
    linked: bool = False
    folder: bool = False
    draft_id: str = ""


class DownloadSelection(BaseModel):
    """Omitted paths copy the source; otherwise copy only explicit relative selections."""

    access: Literal["read_only", "editable"] | None = None
    paths: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    draft_id: str = ""


class DraftSelection(BaseModel):
    """Mapping choice and optional ownership receipt for an unsent link."""

    access: Literal["read_only", "publish_later", "write_through"] | None = None
    confirm_remote: bool = False

    draft_id: str = ""
