"""Hugging Face discovery with immutable revisions and observable download requirements."""

from __future__ import annotations

import re
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelAcquisition(BaseModel):
    """Last observed target receipt, with the immutable location used to reconcile it."""

    id: str
    target_id: str
    storage_root: str
    repository: str
    requested_revision: str
    revision: str | None = None
    destination: str
    state: Literal["queued", "running", "ready", "failed", "cancelled", "interrupted", "stale"]
    phase: str
    bytes_done: int = 0
    bytes_total: int | None = None
    created_at: float
    updated_at: float
    observed_at: float = Field(default_factory=time.time)
    error: str | None = None
    files: list[str] = Field(default_factory=list)
    file_path: str | None = None


class ModelDownloadRequest(BaseModel):
    """A model acquisition request; serving it is a separate operation."""

    model_config = ConfigDict(extra="forbid")
    repository: str = Field(min_length=1, max_length=180)
    revision: str = Field(default="main", min_length=1, max_length=120)
    destination: str = Field(default="", max_length=4096)
    # Exact repository file names to fetch (e.g. one GGUF quantization);
    # empty fetches the whole revision.
    files: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("files")
    @classmethod
    def file_names(cls, value: list[str]) -> list[str]:
        """Accept relative repository file names only, never patterns or escapes."""
        for name in value:
            if not re.fullmatch(r"[\w.+-]+(?:/[\w.+-]+)*", name) or ".." in name.split("/"):
                raise ValueError(f"Invalid model file name: {name!r}")
        return sorted(set(value))

    @field_validator("repository")
    @classmethod
    def repository_id(cls, value: str) -> str:
        """Only accept registry repository identities, never URLs or shell arguments."""
        if not re.fullmatch(r"[A-Za-z0-9][\w.-]*(?:/[A-Za-z0-9][\w.-]*)?", value):
            raise ValueError("Use a Hugging Face repository such as organization/model")
        return value

    @field_validator("revision")
    @classmethod
    def revision_name(cls, value: str) -> str:
        """Accept ordinary tags/branches or a resolved commit without control characters."""
        if not re.fullmatch(r"[\w./-]+", value) or ".." in value:
            raise ValueError("Invalid model revision")
        return value


async def search_models(
    query: str, *, client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """Search downloadable models, keeping unknown size and compatibility unknown."""
    if client is None:
        async with httpx.AsyncClient(timeout=20) as owned:
            return await search_models(query, client=owned)
    response = await client.get(
        "https://huggingface.co/api/models",
        params={
            "search": query[:180],
            "limit": 20,
            "sort": "downloads",
            "direction": -1,
            "full": "true",
        },
    )
    response.raise_for_status()
    return [
        {
            "repository": row["id"],
            "revision": row.get("sha"),
            "task": row.get("pipeline_tag"),
            "gated": row.get("gated", False),
            "downloads": row.get("downloads"),
            "size_bytes": None,
        }
        for row in response.json()
        if isinstance(row, dict) and row.get("id")
    ]
