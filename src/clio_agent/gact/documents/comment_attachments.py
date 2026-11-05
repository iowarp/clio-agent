"""Comments on things that are not versioned artifacts, attached to the next message.

A comment on an agent's answer, an A2UI surface or a page in the in-app browser is an
attachment, not a stored thread (comments plan, "Stored threads or attachments"; ledger FB19).
The composer sends it as one ``comment_session`` part. At admission, before validation, the
part is expanded (ledger FB24a): its marks are rendered into readable entries with the same
entry format as a stored session, appended to the model's text; its images (the labelled
overview, then a crop per region or element) become image parts when the model accepts
images, and otherwise the text says where they were saved. The raw selection never reaches
the model as JSON (addendum A, section 2).

Where images come from (addendum A, section 3): an A2UI surface lives only in the client,
so the browser draws its overview and crops, as ``labelledPng`` does; a browser page's come
from a fresh screenshot on the server, through the surface handler the browser registers.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clio_agent.gact.documents.anchor_text import TextMap, quote_lines
from clio_agent.gact.documents.anchors import ResolvedMark, mark_mode
from clio_agent.gact.documents.models import DocumentAnchor
from clio_agent.gact.documents.review_render import entry_block
from clio_agent.gact.parts import Part

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "AttachmentImage",
    "AttachmentMark",
    "CommentAttachment",
    "CommentAttachmentError",
    "RenderedAttachment",
    "expand_comment_sessions",
    "register_surface",
]

SurfaceKind = Literal["agent-answer", "a2ui-surface", "browser-page"]
_MAX_IMAGE_BASE64 = 12 * 1024 * 1024


class CommentAttachmentError(ValueError):
    """A ``comment_session`` part that does not describe a comment session."""


class AttachmentImage(BaseModel):
    """An image the browser made for the session: the overview (no label) or a mark's crop."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", pattern=r"^(S[1-9][0-9]{0,3})?$")
    data: str = Field(min_length=1, max_length=_MAX_IMAGE_BASE64)


class AttachmentMark(BaseModel):
    """One mark of an attached session."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(pattern=r"^S[1-9][0-9]{0,3}$")
    anchor: DocumentAnchor
    comment: str = Field(default="", max_length=16_384)


class CommentAttachment(BaseModel):
    """What a ``comment_session`` part carries in ``metadata["comment_session"]``."""

    model_config = ConfigDict(extra="forbid")

    kind: SurfaceKind
    title: str = Field(default="", max_length=400)
    marks: list[AttachmentMark] = Field(min_length=1, max_length=32)
    images: list[AttachmentImage] = Field(default_factory=list, max_length=33)


@dataclass
class RenderedAttachment:
    """One attached session as the agent receives it: text and (caption, PNG) images."""

    text: str
    images: list[tuple[str, bytes]] = field(default_factory=list)


SurfaceHandler = Callable[["FastAPI", str, CommentAttachment, Path, bool], RenderedAttachment]
_SURFACES: dict[str, SurfaceHandler] = {}


def register_surface(kind: str, handler: SurfaceHandler) -> None:
    """Let a surface that renders on the server (the in-app browser) render its sessions."""

    _SURFACES[kind] = handler


def _png(data: str) -> bytes:
    try:
        raw = base64.b64decode(data.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CommentAttachmentError("a comment image is not base64 PNG data") from exc
    if not raw.startswith(b"\x89PNG"):
        raise CommentAttachmentError("a comment image is not a PNG")
    return raw


def _box(rect: Sequence[float], what: str) -> str:
    x, y, width, height = rect
    return (
        f"x {round(x * 100)}% to {round((x + width) * 100)}%, "
        f"y {round(y * 100)}% to {round((y + height) * 100)}% of {what}"
    )


def _answer(attachment: CommentAttachment, folder: Path) -> RenderedAttachment:
    """Text marks on an agent's answer: the selection and the text around it."""

    first = attachment.marks[0].anchor.source
    message = str(first.get("message_id") or "")
    header = f"Comment on your earlier answer{f' (message {message})' if message else ''}."
    blocks = [header]
    for mark in attachment.marks:
        anchor = mark.anchor
        text = f"{anchor.prefix}{anchor.exact}{anchor.suffix}"
        start = len(anchor.prefix)
        mapped = TextMap(text, list(range(len(text))), text)
        lines = [
            line
            for line in quote_lines(mapped, start, start + len(anchor.exact), source_name="")
            if line[0] != "Source"
        ]
        resolved = ResolvedMark(mode="text", lines=lines, under=anchor.exact)
        blocks.append(
            entry_block(mark.label, resolved, mark.comment, image_number=None, overflow_dir=folder)
        )
    return RenderedAttachment("\n\n".join(blocks))


def _surface_header(attachment: CommentAttachment) -> list[str]:
    source = attachment.marks[0].anchor.source
    title = attachment.title or str(source.get("component") or "an A2UI surface")
    lines = [
        f"Comment on {title} (A2UI surface {source.get('surface', '?')}, "
        f"revision {source.get('revision', '?')})."
    ]
    detail = []
    if source.get("component"):
        detail.append(f"component {source['component']}")
    if source.get("data_reference"):
        detail.append(f"data {source['data_reference']}")
    if source.get("filters"):
        detail.append(f"filters {source['filters']}")
    if source.get("selection"):
        detail.append(f"selection {source['selection']}")
    if source.get("view"):
        detail.append(f"view {source['view']}")
    if detail:
        lines.append("Shown: " + "; ".join(str(item) for item in detail) + ".")
    return lines


def _a2ui(attachment: CommentAttachment, folder: Path, images: bool) -> RenderedAttachment:
    """Regions on an A2UI surface: the browser's overview and crops, and the data under each
    box as readable lines (rows, features, nodes, visible text)."""

    pictures: list[tuple[str, bytes]] = []
    overview = [image for image in attachment.images if not image.label]
    labels = [mark.label for mark in attachment.marks]
    for image in overview[:1]:
        png = _png(image.data)
        (folder / "overview.png").write_bytes(png)
        pictures.append((f"the view with {', '.join(labels)} drawn on it", png))
    crops = {image.label: _png(image.data) for image in attachment.images if image.label}
    numbers: dict[str, int] = {}
    for label in labels:
        if label in crops:
            (folder / f"{label}.png").write_bytes(crops[label])
            pictures.append((f"{label} at twice its size", crops[label]))
            numbers[label] = len(pictures)
    lines = _surface_header(attachment)
    if pictures:
        if images:
            if overview:
                lines.append(f"Image 1 is {pictures[0][0]}.")
        else:
            lines.append(
                "Images were not sent: the selected model does not accept images. The overview "
                f"and crops are saved in {folder}."
            )
    blocks = ["\n".join(lines)]
    for mark in attachment.marks:
        anchor = mark.anchor
        resolved = ResolvedMark(mode=mark_mode(anchor))
        if anchor.rect:
            resolved.lines.append(("Box", _box(anchor.rect, "the view")))
        bounds = anchor.source.get("geographic_bounds")
        if isinstance(bounds, dict) and bounds:
            resolved.lines.append(
                (
                    "Area",
                    f"west {bounds.get('west')}, north {bounds.get('north')}, "
                    f"east {bounds.get('east')}, south {bounds.get('south')}",
                )
            )
        under = anchor.source.get("under")
        under_lines = [str(line) for line in under] if isinstance(under, list) else []
        resolved.under = "\n".join(under_lines)
        if under_lines:
            resolved.lines.append(("Under", resolved.under))
        blocks.append(
            entry_block(
                mark.label,
                resolved,
                mark.comment,
                image_number=numbers.get(mark.label) if images else None,
                overflow_dir=folder,
            )
        )
    return RenderedAttachment("\n\n".join(blocks), pictures if images else [])


def _folder(session_id: str) -> Path:
    from clio_agent.runtime.session_scratch import session_scratch_dir  # noqa: PLC0415

    base = session_scratch_dir(session_id)
    root = Path(base) if base is not None else Path.cwd() / ".clio-comments"
    folder = root / "comments" / f"att_{uuid.uuid4().hex[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def render_attachment(
    app: "FastAPI", session_id: str, attachment: CommentAttachment, *, images: bool
) -> RenderedAttachment:
    """One attached session rendered for the agent."""

    folder = _folder(session_id)
    if attachment.kind == "agent-answer":
        return _answer(attachment, folder)
    if attachment.kind == "a2ui-surface":
        return _a2ui(attachment, folder, images)
    handler = _SURFACES.get(attachment.kind)
    if handler is None:
        text = (
            f"Comments on {attachment.title or attachment.kind} could not be rendered: "
            f"surface_unavailable: no renderer for {attachment.kind} sessions is loaded."
        )
        return RenderedAttachment(text)
    return handler(app, session_id, attachment, folder, images)


_KIND_WORDS = {
    "agent-answer": "your earlier answer",
    "a2ui-surface": "a view",
    "browser-page": "a page",
}


def _transcript_summary(attachment: CommentAttachment) -> str:
    """The person's marks as their transcript shows them: what each points at, and the
    comment. The agent reads the rendered entries instead (``internal_model_text``)."""

    title = attachment.title or _KIND_WORDS[attachment.kind]
    lines = [f"Comments on {title}:"]
    for mark in attachment.marks:
        lines.append(f"{mark.label}: {mark.comment.strip() or '(no comment)'}")
    return "\n".join(lines)


def expand_comment_sessions(
    app: "FastAPI", session: Any, parts: list[Part], *, images: bool
) -> tuple[list[Part], str]:
    """Expand ``comment_session`` parts into image parts and the text the model reads.

    Each part keeps its marks (for the transcript) but loses its image data, which moves to
    image parts after it. Returns the new parts and the rendered text to append to the
    model's text (empty when the message has no comment session).

    Raises:
        CommentAttachmentError: a ``comment_session`` part does not validate.
    """

    out: list[Part] = []
    texts: list[str] = []
    for part in parts:
        if part.type != "comment_session":
            out.append(part)
            continue
        try:
            attachment = CommentAttachment.model_validate(part.metadata.get("comment_session"))
        except ValidationError as exc:
            raise CommentAttachmentError(f"invalid comment session: {exc.errors()[:3]}") from exc
        rendered = render_attachment(app, session.id, attachment, images=images)
        texts.append(rendered.text)
        kept = attachment.model_dump(mode="json")
        kept["images"] = []
        out.append(part.model_copy(update={"metadata": {**part.metadata, "comment_session": kept}}))
        out.append(
            Part(
                id=f"{part.id or 'comment'}-summary",
                type="text",
                text=_transcript_summary(attachment),
                metadata={"comment_summary": True},
            )
        )
        for index, (caption, png) in enumerate(rendered.images, start=1):
            out.append(
                Part(
                    id=f"{part.id or 'comment'}-image-{index}",
                    type="image",
                    data=base64.b64encode(png).decode("ascii"),
                    media_type="image/png",
                    metadata={"comment_session": part.id, "caption": caption},
                )
            )
    return out, "\n\n".join(texts)
