"""Typed document-artifact wire and persistence models."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

DocumentProfile = Literal[
    "markdown",
    "pdf",
    "latex",
    "html-static",
    "ooxml-word",
    "ooxml-sheet",
    "ooxml-slides",
    "odf-text",
    "odf-sheet",
    "odf-slides",
    "binary",
]
AnchorProfile = Literal[
    "text-quote",
    "pdf-quad",
    "dom",
    "sheet-range",
    "slide-shape",
    "native-comment",
    "source-map",
]
ReviewStatus = Literal["queued", "dispatched", "human-note", "failed", "stale"]
WorkingCopyStatus = Literal["active", "conflict", "closed", "missing", "error"]
EditorProvider = Literal["native", "onlyoffice", "collabora"]


class DocumentAnchor(BaseModel):
    """One immutable, artifact-version-bound document selection."""

    model_config = ConfigDict(extra="forbid")

    profile: AnchorProfile
    exact: str = ""
    prefix: str = ""
    suffix: str = ""
    source_path: str = ""
    start: Optional[int] = Field(default=None, ge=0)
    end: Optional[int] = Field(default=None, ge=0)
    page_index: Optional[int] = Field(default=None, ge=0)
    quads: list[list[float]] = Field(default_factory=list)
    selector: str = ""
    stable_id: str = ""
    sheet: str = ""
    cell_range: str = ""
    slide_id: str = ""
    shape_id: str = ""
    native_comment_id: str = ""
    source: dict[str, Any] = Field(default_factory=dict)

    @field_validator("quads")
    @classmethod
    def validate_quads(cls, value: list[list[float]]) -> list[list[float]]:
        """Require normalized PDF quadrilaterals."""

        for quad in value:
            if len(quad) != 8 or any(point < 0.0 or point > 1.0 for point in quad):
                raise ValueError("each quad must contain eight normalized coordinates")
        return value


class ArtifactReview(BaseModel):
    """A durable user-to-agent review instruction for one artifact version."""

    model_config = ConfigDict(frozen=True)

    id: str
    session_id: str
    workspace_id: str
    artifact_id: str
    artifact_name: str
    artifact_version: int = Field(ge=1)
    artifact_sha256: str
    anchor: DocumentAnchor
    text: str
    status: ReviewStatus
    native: bool = False
    native_text_hash: str = ""
    idempotency_key: str = ""
    message_id: str = ""
    created_at: str
    error: str = ""


class CreateArtifactReviewRequest(BaseModel):
    """Create and immediately dispatch one CLIO review instruction."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    expected_version: int = Field(ge=1)
    expected_sha256: str
    anchor: DocumentAnchor
    text: str = Field(min_length=1, max_length=16_384)
    idempotency_key: str = Field(min_length=8, max_length=200)
    allow_historical: bool = False


class CreateWorkingCopyRequest(BaseModel):
    """Materialize an exact artifact version for desktop or embedded editing."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    provider: EditorProvider = "native"
    writable: bool = True
    auto_checkpoint: bool = True


class DocumentWorkingCopy(BaseModel):
    """A confined mutable file whose stable saves mint immutable revisions."""

    id: str
    session_id: str
    workspace_id: str
    artifact_name: str
    base_artifact_id: str
    head_artifact_id: str
    base_version: int = Field(ge=1)
    head_version: int = Field(ge=1)
    base_sha256: str
    last_sha256: str
    path: str
    provider: EditorProvider
    writable: bool
    auto_checkpoint: bool
    status: WorkingCopyStatus = "active"
    created_at: str
    updated_at: str
    last_checkpoint_at: str = ""
    conflict_head_artifact_id: str = ""
    conflict_candidate_sha256: str = ""
    error: str = ""
    native_comment_fingerprints: list[str] = Field(default_factory=list)


class ResolveWorkingCopyConflictRequest(BaseModel):
    """Resolve a stale working-copy save without silently overwriting the head."""

    model_config = ConfigDict(extra="forbid")

    resolution: Literal["keep-current", "use-working-copy"]
    expected_head_artifact_id: str


class CreateRenditionRequest(BaseModel):
    """Request a deterministic derived rendition."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["pdf"] = "pdf"


class DocumentManifest(BaseModel):
    """Viewer routing manifest for one immutable artifact version."""

    artifact_id: str
    workspace_id: str
    name: str
    version: int
    sha256: str
    mime_type: str
    profile: DocumentProfile
    content_url: str
    anchors: list[AnchorProfile]
    native_open: bool
    embedded_editors: list[EditorProvider] = Field(default_factory=list)
    rendition_formats: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class CreateEditorSessionRequest(BaseModel):
    """Open one embedded editor against an existing working copy."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["onlyoffice", "collabora"]


class DocumentEditorSession(BaseModel):
    """A short-lived, working-copy-scoped embedded editor session."""

    id: str
    working_copy_id: str
    provider: Literal["onlyoffice", "collabora"]
    status: Literal["ready", "unavailable", "closed"]
    editor_url: str = ""
    token: str = ""
    expires_at: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
