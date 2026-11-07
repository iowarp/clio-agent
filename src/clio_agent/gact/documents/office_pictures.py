"""Name the pictures a region covers on a PowerPoint or Word rendition (ledger FB16).

A region on a slide or page that holds a figure is usually about that figure, and a figure in
a document is usually a workspace image a script made. So the entry names the picture in the
file's own terms, and when the picture's bytes are a workspace image artifact, names that
artifact and what produced it: the fix belongs in the script, then the document is rebuilt.

PowerPoint: rendition page N is slide N, and a picture shape's box over the slide size is in
the same normalized coordinates as the region. Word: inline pictures have no absolute place,
so the rendition's own image placements (the content stream's ``cm`` before ``Do``) are read
and matched in order to the document's pictures (Phase 0, measurement 5).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.documents.anchors import MarkTarget

__all__ = ["Picture", "picture_lines", "pictures_under"]

_MAX_PICTURES = 3
_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


@dataclass(frozen=True)
class Picture:
    """One picture under a region: where it is in the file and its bytes' SHA-256."""

    where: str
    sha256: str


def _overlaps(a: Sequence[float], b: Sequence[float]) -> bool:
    return not (
        a[0] > b[0] + b[2] or a[0] + a[2] < b[0] or a[1] > b[1] + b[3] or a[1] + a[3] < b[1]
    )


def _slide_pictures(path: Path, page_index: int, rect: Sequence[float]) -> list[Picture]:
    from pptx import Presentation  # noqa: PLC0415 - optional heavy import
    from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: PLC0415

    deck = Presentation(str(path))
    slides = list(deck.slides)
    if not 0 <= page_index < len(slides):
        return []
    width, height = deck.slide_width or 1, deck.slide_height or 1
    found: list[Picture] = []
    for shape in slides[page_index].shapes:
        if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
            continue
        box = (
            (shape.left or 0) / width,
            (shape.top or 0) / height,
            (shape.width or 0) / width,
            (shape.height or 0) / height,
        )
        if _overlaps(box, rect):
            digest = hashlib.sha256(shape.image.blob).hexdigest()
            found.append(Picture(f'shape {shape.shape_id} "{shape.name}"', digest))
    return found


def _multiply(m: Sequence[float], n: Sequence[float]) -> list[float]:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return [
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    ]


def _placements(pdf: Path) -> list[list[tuple[float, float, float, float]]]:
    """Each rendition page's image boxes, normalized and top-left, in drawing order."""

    from pypdf import PdfReader  # noqa: PLC0415 - optional heavy import
    from pypdf.generic import ContentStream  # noqa: PLC0415

    reader = PdfReader(str(pdf))
    pages: list[list[tuple[float, float, float, float]]] = []
    for page in reader.pages:
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        xobjects = (page.get("/Resources") or {}).get("/XObject") or {}
        stack = [[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]]
        placed: list[tuple[float, float, float, float]] = []
        contents = page.get_contents()
        operations = ContentStream(contents, reader).operations if contents is not None else []
        for operands, operator in operations:
            if operator == b"q":
                stack.append(list(stack[-1]))
            elif operator == b"Q" and len(stack) > 1:
                stack.pop()
            elif operator == b"cm":
                stack[-1] = _multiply([float(v) for v in operands], stack[-1])
            elif operator == b"Do" and operands and operands[0] in xobjects:
                if xobjects[operands[0]].get_object().get("/Subtype") != "/Image":
                    continue
                a, _b, _c, d, e, f = stack[-1]
                placed.append((e / width, 1 - (f + d) / height, a / width, d / height))
        pages.append(placed)
    return pages


def _word_pictures(
    path: Path, pdf: Path, page_index: int, rect: Sequence[float]
) -> tuple[list[Picture], str]:
    from docx import Document  # noqa: PLC0415 - optional heavy import

    pages = _placements(pdf)
    if not 0 <= page_index < len(pages):
        return [], ""
    document = Document(str(path))
    pictures: list[tuple[int, bytes]] = []
    for number, paragraph in enumerate(document.paragraphs, start=1):
        for blip in paragraph._p.xpath(".//a:blip"):
            part = document.part.related_parts.get(blip.get(_EMBED))
            if part is not None:
                pictures.append((number, part.blob))
    drawn = sum(len(page) for page in pages)
    if drawn != len(pictures):
        return [], (
            f"picture_unmatched: the rendition draws {drawn} images and the document holds "
            f"{len(pictures)} pictures, so they cannot be matched in order"
        )
    before = sum(len(page) for page in pages[:page_index])
    found: list[Picture] = []
    for offset, box in enumerate(pages[page_index]):
        if _overlaps(box, rect):
            index = before + offset
            held_in, blob = pictures[index]
            where = f"picture {index + 1} of the document, in paragraph {held_in}"
            found.append(Picture(where, hashlib.sha256(blob).hexdigest()))
    return found, ""


def pictures_under(
    target: "MarkTarget", page_index: int, rect: Sequence[float]
) -> tuple[list[Picture], str]:
    """The pictures ``rect`` covers on rendition page ``page_index``, and a typed note."""

    if target.profile == "ooxml-slides":
        return _slide_pictures(target.path, page_index, rect), ""
    if target.profile == "ooxml-word":
        return _word_pictures(target.path, target.view_path, page_index, rect)
    return [], ""


def _workspace_image(target: "MarkTarget", sha256: str) -> tuple[Any, Any] | None:
    """The newest workspace artifact version with these bytes, other than the document."""

    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    best: tuple[Any, Any] | None = None
    for record in get_registry(target.app).list_for_workspace(target.record.workspace_id):
        if record.name == target.record.name:
            continue
        for version in record.versions:
            if version.sha256 == sha256 and (
                best is None or version.created_at > best[1].created_at
            ):
                best = (record, version)
    return best


def picture_lines(
    target: "MarkTarget", page_index: int, rect: Sequence[float]
) -> tuple[list[tuple[str, str]], list[str]]:
    """The entry's Picture lines for a region on a rendition, with any typed notes."""

    from clio_agent.gact.documents.produced_by import produced_by  # noqa: PLC0415

    try:
        found, note = pictures_under(target, page_index, rect)
    except Exception as exc:  # noqa: BLE001 - forwards: into the entry, as the note on a damaged package
        return [], [f"picture_unreadable: {type(exc).__name__}: {exc}"]
    lines: list[tuple[str, str]] = []
    for picture in found[:_MAX_PICTURES]:
        match = _workspace_image(target, picture.sha256)
        if match is None:
            lines.append(("Picture", f"{picture.where}; its bytes match no workspace image"))
            continue
        record, version = match
        lines.append(
            (
                "Picture",
                f"{picture.where}: the workspace image {record.name} "
                f"(artifact {version.artifact_id}, version {version.version})",
            )
        )
        made = produced_by(
            target.app, version, workspace_id=target.record.workspace_id, noun="picture"
        )
        text = "\n".join(made.lines)
        lines.append(("Made by", text.removeprefix("Produced by: ")))
        if made.known:
            lines.append(("Then", f"rebuild {target.name} so it embeds the new picture."))
    if len(found) > _MAX_PICTURES:
        lines.append(("Pictures", f"{len(found) - _MAX_PICTURES} more under the region"))
    return lines, [note] if note else []
