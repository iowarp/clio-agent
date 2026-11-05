"""What the agent receives for one comment session: readable entries, an overview, crops.

Replaces the old review prompt, which dumped the anchor as JSON (comments plan, "What the
agent receives"; addendum A, section 2). For one session on one target it writes:

* one header naming the target, its version and, for a figure or page a script made, the
  call and script that made it ("Produced by"), or that the producer is unknown;
* one labelled overview per page that holds a mark (the whole image for an image), drawn
  with every mark, as the first image parts;
* one entry per mark, headed by its label (``S1 (region, attached as image 2)``), with the
  lines its resolver produced, the text under the mark cut to ``comments.under_mark_chars``
  with the rest in a session file the entry names, any typed note, and the comment;
* one crop per region or element, after the overviews, in label order.

When the model does not accept images, no image part goes and the header says where the
overview and crops were saved instead (ledger FB24).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from clio_agent.gact.documents.anchors import MarkTarget, ResolvedMark, mark_mode, resolver_for
from clio_agent.gact.documents.comment_settings import under_mark_chars
from clio_agent.gact.documents.mark_images import MarkBox, labelled_overview_png
from clio_agent.gact.documents.models import DocumentAnchor
from clio_agent.gact.documents.produced_by import produced_by

__all__ = [
    "MarkInput",
    "RenderedImage",
    "RenderedSession",
    "entry_block",
    "joined_labels",
    "render_session",
]

_LABEL_WIDTH = 10
_MODE_NOUN = {"text": "text", "region": "region", "element": "element", "native": "comment"}
_FIGURE_PROFILES = {"image", "html-static"}


@dataclass(frozen=True)
class MarkInput:
    """One mark of a session: its label, what it points at, and the person's comment."""

    label: str
    anchor: DocumentAnchor
    comment: str


@dataclass(frozen=True)
class RenderedImage:
    """One image part: a labelled overview or a mark's crop, with the file it was saved to."""

    caption: str
    png: bytes
    path: Path


@dataclass
class RenderedSession:
    """The session's text and its images, in the order the text numbers them."""

    text: str
    images: list[RenderedImage] = field(default_factory=list)
    marks: dict[str, ResolvedMark] = field(default_factory=dict)


def _line(label: str, value: str) -> str:
    head = f"  {label}:".ljust(_LABEL_WIDTH + 2)
    rest = value.split("\n")
    return "\n".join([head + rest[0], *(" " * (_LABEL_WIDTH + 2) + more for more in rest[1:])])


def _safe(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", label) or "mark"


def entry_block(
    label: str,
    mark: ResolvedMark,
    comment: str,
    *,
    image_number: int | None,
    overflow_dir: Path | None,
) -> str:
    """One mark's entry: its heading, resolver lines, cut under-text, notes and comment."""

    noun = _MODE_NOUN[mark.mode]
    heading = (
        f"{label} ({noun}, attached as image {image_number})"
        if image_number
        else (f"{label} ({noun})")
    )
    lines = [heading]
    limit = under_mark_chars()
    for name, value in mark.lines:
        if name == "Under" and len(value) > limit:
            kept = value[:limit].rstrip()
            where = ""
            if overflow_dir is not None:
                overflow_dir.mkdir(parents=True, exist_ok=True)
                spill = overflow_dir / f"{_safe(label)}-under.txt"
                spill.write_text(mark.under, encoding="utf-8")
                where = f" The rest is in {spill}."
            lines.append(_line(name, kept + " ..."))
            lines.append(_line("Cut", f"at {limit:,} of {len(value):,} characters.{where}"))
            continue
        lines.append(_line(name, value))
    for note in mark.notes:
        lines.append(_line("Note", note))
    lines.append(_line("Comment", comment.strip() or "(no comment)"))
    return "\n".join(lines)


def _header(target: MarkTarget) -> list[str]:
    version = target.version
    sha = version.sha256 or ""
    short = f"{sha[:4]}...{sha[-4:]}" if len(sha) >= 8 else sha
    identity = f"artifact {version.artifact_id}, version {version.version}, sha256 {short}"
    if target.via_rendition:
        return [
            f"Review of {target.name}, viewed as its PDF rendition",
            f"({identity}). Edit {target.name}, not the PDF.",
        ]
    lines = [f"Review of {target.name} ({identity})."]
    if target.profile in _FIGURE_PROFILES:
        noun = "image" if target.profile == "image" else "page"
        lines.extend(
            produced_by(
                target.app, version, workspace_id=target.record.workspace_id, noun=noun
            ).lines
        )
    return lines


def render_session(
    target: MarkTarget,
    marks: Sequence[MarkInput],
    *,
    images: bool,
    folder: Path,
) -> RenderedSession:
    """Resolve every mark of one session on ``target`` and write what the agent receives.

    ``folder`` (a session scratch folder) receives the overview and crop PNGs and any
    under-mark overflow, whether or not images go with the message.
    """

    resolved: dict[str, ResolvedMark] = {}
    for mark in marks:
        resolver = resolver_for(target.profile, mark.anchor.profile)
        if resolver is None:
            resolved[mark.label] = ResolvedMark(
                mode=mark_mode(mark.anchor),
                notes=[
                    f"anchor_unsupported: {mark.anchor.profile} marks are not resolved on "
                    f"{target.profile} documents"
                ],
            )
            continue
        resolved[mark.label] = resolver.resolve(target, mark.anchor)

    folder.mkdir(parents=True, exist_ok=True)
    rendered = RenderedSession(text="", marks=resolved)
    pages: dict[int | None, list[str]] = {}
    for mark in marks:
        found = resolved[mark.label]
        if found.rect is not None:
            pages.setdefault(found.page_index, []).append(mark.label)
    by_label = {mark.label: mark for mark in marks}
    for mark in marks:
        if mark.anchor.viewport_width:
            target.cache.setdefault("viewport_width", mark.anchor.viewport_width)
    paged = target.profile not in {"image", "html-static"}
    for page, labels in sorted(pages.items(), key=lambda item: (item[0] is None, item[0] or 0)):
        image = None
        for label in labels:
            resolver = resolver_for(target.profile, by_label[label].anchor.profile)
            image = resolver.page_image(target, page) if resolver is not None else None
            if image is not None:
                break
        if image is None:
            continue
        boxes = [MarkBox(label, resolved[label].rect) for label in labels]  # type: ignore[arg-type]
        what = (
            f"page {page + 1}"
            if paged and page is not None
            else ("the figure" if target.profile == "image" else "the page")
        )
        caption = f"{what} with {joined_labels(labels)} drawn on it"
        name = f"overview-{'p' + str(page + 1) if paged and page is not None else 'all'}.png"
        png = labelled_overview_png(image, boxes)
        path = folder / name
        path.write_bytes(png)
        rendered.images.append(RenderedImage(caption, png, path))
    numbers: dict[str, int] = {}
    for mark in marks:
        crop = resolved[mark.label].crop
        if not crop:
            continue
        path = folder / f"{_safe(mark.label)}.png"
        path.write_bytes(crop)
        rendered.images.append(RenderedImage(f"{mark.label} at twice its size", crop, path))
        numbers[mark.label] = len(rendered.images)

    lines = _header(target)
    if rendered.images:
        if images:
            for index, part in enumerate(rendered.images, start=1):
                if not part.caption.startswith(tuple(m.label + " " for m in marks)):
                    lines.append(f"Image {index} is {part.caption}.")
        else:
            lines.append(
                "Images were not sent: the selected model does not accept images. The overview "
                f"and crops are saved in {folder}."
            )
    blocks = ["\n".join(lines)]
    for mark in marks:
        blocks.append(
            entry_block(
                mark.label,
                resolved[mark.label],
                mark.comment,
                image_number=numbers.get(mark.label) if images else None,
                overflow_dir=folder,
            )
        )
    rendered.text = "\n\n".join(blocks)
    if not images:
        rendered.images = []
    return rendered


def joined_labels(labels: Sequence[str]) -> str:
    """ "S1", "S1 and S2", "S1, S2 and S3"."""

    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]
