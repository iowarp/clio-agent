"""Document-artifact extensions for the additive GACT wire models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DocumentCapabilityFields(BaseModel):
    """Capability metadata for document viewing, review, and editing."""

    x_clio_document_artifacts: dict[str, Any] = Field(default_factory=dict)


class DocumentPartFields(BaseModel):
    """Version-bound document review fields carried by a message part."""

    review_id: str = ""
    artifact_id: str = ""
    artifact_version: int = 0
    artifact_sha256: str = ""
    review_text: str = ""
    anchor: dict[str, Any] = Field(default_factory=dict)


__all__ = ["DocumentCapabilityFields", "DocumentPartFields"]
