"""Typed document-artifact wire and persistence models."""

from __future__ import annotations

from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    "image",
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
    "region",
]
#: Every profile a document artifact can have, and every anchor a comment can carry, as
#: advertised in the ``x_clio_document_artifacts`` capability.
DOCUMENT_PROFILES: tuple[str, ...] = tuple(p for p in get_args(DocumentProfile) if p != "binary")
ANCHOR_PROFILES: tuple[str, ...] = get_args(AnchorProfile)
ReviewStatus = Literal[
    "draft", "queued", "dispatched", "human-note", "failed", "stale", "discarded"
]
ThreadAnchorState = Literal["current", "moved", "outdated"]
WorkingCopyStatus = Literal["active", "conflict", "closed", "missing", "error"]


class DocumentAnchor(BaseModel):
    """What one comment points at, in the coordinates of the thing it was made on.

    The one selection model for comments, reviews and attachments (comments plan rule 1).
    ``text-quote`` and ``pdf-quad`` carry text; ``region`` a normalized rectangle (``rect``
    as x, y, width, height in 0..1 of the page or image shown, with ``page_index`` when
    paged and the natural size of what was shown); ``dom`` a picked element (``selector``,
    ``tag``, excerpts, and its ``rect`` in CSS pixels of a ``viewport_width`` wide view).
    ``source`` names a target that is not an artifact: a browser page (``page``:
    ``{id, url, title}``, ``captured_at``), an agent answer (``message_id``) or an A2UI
    surface (``surface``, ``component_id``, ``revision``, and the data under the mark).
    """

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
    rect: list[float] = Field(default_factory=list)
    natural_width: Optional[int] = Field(default=None, ge=1)
    natural_height: Optional[int] = Field(default=None, ge=1)
    crop_asset: str = ""
    tag: str = Field(default="", max_length=64)
    text_excerpt: str = Field(default="", max_length=4_000)
    html_excerpt: str = Field(default="", max_length=16_000)
    viewport_width: Optional[int] = Field(default=None, ge=1)
    source: dict[str, Any] = Field(default_factory=dict)

    @field_validator("quads")
    @classmethod
    def validate_quads(cls, value: list[list[float]]) -> list[list[float]]:
        """Require normalized PDF quadrilaterals."""

        for quad in value:
            if len(quad) != 8 or any(point < 0.0 or point > 1.0 for point in quad):
                raise ValueError("each quad must contain eight normalized coordinates")
        return value

    @model_validator(mode="after")
    def validate_rect(self) -> "DocumentAnchor":
        """A region's rectangle is normalized and inside what was shown; an element's is
        in CSS pixels of its view."""

        if self.profile == "region" and len(self.rect) != 4:
            raise ValueError("a region anchor needs rect as [x, y, width, height]")
        if not self.rect:
            return self
        if len(self.rect) != 4:
            raise ValueError("rect must be [x, y, width, height]")
        x, y, width, height = self.rect
        if width <= 0 or height <= 0:
            raise ValueError("rect must have a positive width and height")
        if self.profile == "region" and (
            x < 0.0 or y < 0.0 or x + width > 1.0 + 1e-6 or y + height > 1.0 + 1e-6
        ):
            raise ValueError("a region's rect must lie within 0..1 of what was shown")
        return self


class ArtifactReview(BaseModel):
    """One comment thread on one artifact version: the mark, the comment, the agent's reply.

    A select session's marks become threads sharing ``session_key``, labelled ``S1``, ``S2``
    and onward per artifact version (addendum A, section 1). A ``draft`` is a mark not yet
    sent; ``human-note`` one sent with "Send to agent" off. When the turn the session
    started finishes, its final message is stored as ``reply_text`` on every thread of the
    session and the version it made as ``reply_version_artifact_id``. A newer version
    re-anchors the thread: ``anchor_state`` is ``moved`` (``current_anchor`` on
    ``current_artifact_id``) or ``outdated``; a region stays ``current`` on its version.
    Rows are append-only projections in the workspace's reviews ledger.
    """

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
    label: str = ""
    session_key: str = ""
    send_to_agent: bool = True
    reply_text: str = ""
    reply_message_id: str = ""
    reply_version_artifact_id: str = ""
    reply_error: str = ""
    anchor_state: ThreadAnchorState = "current"
    current_artifact_id: str = ""
    current_anchor: Optional[DocumentAnchor] = None
    updated_at: str = ""


class CommentMark(BaseModel):
    """One mark of a select session: what it points at and the person's comment."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", pattern=r"^(S[1-9][0-9]{0,3})?$")
    anchor: DocumentAnchor
    comment: str = Field(default="", max_length=16_384)
    send_to_agent: bool = True


class CommentSessionRequest(BaseModel):
    """Send one select session's marks on one artifact version together, as one turn."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    expected_version: int = Field(ge=1)
    expected_sha256: str
    marks: list[CommentMark] = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=8, max_length=200)
    allow_historical: bool = False


class CommentDraftsRequest(BaseModel):
    """Keep a session's unsent marks as a draft on one artifact version (ledger FB25)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1)
    expected_sha256: str
    marks: list[CommentMark] = Field(default_factory=list, max_length=32)


class CommentTargetRequest(BaseModel):
    """A workspace file the person wants to comment on (ledger FB22)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4_096)
    session_id: str = Field(min_length=1)


class CreateWorkingCopyRequest(BaseModel):
    """Materialize an exact artifact version for editing in its desktop application."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
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
    rendition_formats: list[str] = Field(default_factory=list)
    pdf_rendition_artifact_id: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)
