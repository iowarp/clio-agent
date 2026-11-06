"""Map a mark on an Office file's PDF rendition back to the source file's own structure.

A comment on a rendition is about the source (comments plan, rule 3), so the entry names
where in the source it points: the paragraph and its heading in a Word file, the slide and
shapes in a PowerPoint file, the cells in an Excel file. OpenDocument files have no such map
here; their entries name the rendition page only, and say so.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from clio_agent.gact.documents.models import DocumentProfile

__all__ = ["NativeCommentResolver", "office_source_line", "register_builtin"]

_WORD = re.compile(r"\w+", re.UNICODE)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _words(text: str) -> set[str]:
    return {word.lower() for word in _WORD.findall(text)}


def _overlaps(a: Sequence[float], b: Sequence[float]) -> bool:
    return not (
        a[0] > b[0] + b[2] or a[0] + a[2] < b[0] or a[1] > b[1] + b[3] or a[1] + a[3] < b[1]
    )


def _word_paragraph(path: Path, text: str) -> str | None:
    from docx import Document  # noqa: PLC0415 - optional heavy import

    document = Document(str(path))
    needle = _collapse(text).lower()
    wanted = _words(text)
    best: tuple[float, int] | None = None
    heading = ""
    headings: dict[int, str] = {}
    paragraphs = [p for p in document.paragraphs if p.text.strip()]
    for index, paragraph in enumerate(paragraphs, start=1):
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        if style.startswith(("Heading", "Title")):
            heading = _collapse(paragraph.text)
        headings[index] = heading
        body = _collapse(paragraph.text).lower()
        if needle and needle[:120] in body:
            score = 2.0
        elif wanted:
            score = len(wanted & _words(body)) / len(wanted)
        else:
            continue
        if best is None or score > best[0]:
            best = (score, index)
    if best is None or best[0] < 0.5:
        return None
    index = best[1]
    under = headings.get(index, "")
    return f'paragraph {index}, under heading "{under}"' if under else f"paragraph {index}"


def _slide_shapes(path: Path, page_index: int, rect: Sequence[float] | None, text: str) -> str:
    from pptx import Presentation  # noqa: PLC0415 - optional heavy import

    deck = Presentation(str(path))
    slides = list(deck.slides)
    if not 0 <= page_index < len(slides):
        return f"page {page_index + 1} of the rendition (the deck has {len(slides)} slides)"
    slide = slides[page_index]
    width, height = deck.slide_width or 1, deck.slide_height or 1
    needle = _collapse(text).lower()
    named: list[str] = []
    for shape in slide.shapes:
        box = (
            (shape.left or 0) / width,
            (shape.top or 0) / height,
            (shape.width or 0) / width,
            (shape.height or 0) / height,
        )
        has_text = getattr(shape, "has_text_frame", False) and shape.has_text_frame
        body = _collapse(shape.text_frame.text).lower() if has_text else ""
        if (rect is not None and _overlaps(box, rect)) or (needle and needle[:80] in body):
            named.append(f'shape {shape.shape_id} "{shape.name}"')
    where = f"slide {page_index + 1}"
    if not named:
        return where
    more = f" and {len(named) - 3} more shapes" if len(named) > 3 else ""
    return f"{where}, {', '.join(named[:3])}{more}"


def _sheet_cells(path: Path, text: str) -> str | None:
    from openpyxl import load_workbook  # noqa: PLC0415 - optional heavy import
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    try:
        wanted = _words(text)
        needle = _collapse(text).lower()
        for sheet in workbook.worksheets:
            hits: list[tuple[int, int]] = []
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None or not hasattr(cell, "column"):
                        continue
                    value = _collapse(str(cell.value)).lower()
                    if not value:
                        continue
                    if (needle and needle in value) or (value in wanted and len(value) > 1):
                        hits.append((int(cell.row), int(cell.column)))
            if hits:
                rows = [r for r, _c in hits]
                cols = [c for _r, c in hits]
                first = f"{get_column_letter(min(cols))}{min(rows)}"
                last = f"{get_column_letter(max(cols))}{max(rows)}"
                cells = f"cell {first}" if first == last else f"cells {first}:{last}"
                return f"sheet {sheet.title}, {cells}"
        return None
    finally:
        workbook.close()


def office_source_line(
    profile: DocumentProfile,
    path: Path,
    *,
    page_index: int | None,
    rect: Sequence[float] | None,
    text: str,
) -> tuple[str | None, str | None]:
    """The Source line for a mark on an Office rendition, and a typed note when there is none.

    Returns ``(source, note)``: ``source`` names where in the Office file the mark points;
    ``note`` says why it could not be named.
    """

    page = f"page {page_index + 1} of the rendition" if page_index is not None else "the rendition"
    try:
        if profile == "ooxml-word":
            found = _word_paragraph(path, text) if text.strip() else None
            if found:
                return found, None
            return page, "source_unmapped: no paragraph of the Word file matches the marked text"
        if profile == "ooxml-slides" and page_index is not None:
            return _slide_shapes(path, page_index, rect, text), None
        if profile == "ooxml-sheet":
            found = _sheet_cells(path, text) if text.strip() else None
            if found:
                return found, None
            return page, "source_unmapped: no cell of the workbook matches the marked text"
        if profile.startswith("odf-"):
            return page, "source_unmapped: OpenDocument files have no position map here"
    except Exception as exc:  # noqa: BLE001 - forwards: into the entry, as the note on a damaged package
        return page, f"source_unreadable: {type(exc).__name__}: {exc}"
    return page, None


class NativeCommentResolver:
    """``@clio`` comments written in an Office or OpenDocument file itself.

    The comment's place in the file is what the package records: a cell for a workbook
    comment, otherwise the comment's own identifier. There is no crop; the file is the
    target and the comment text is the instruction.
    """

    def resolve(self, target: Any, anchor: Any) -> Any:
        from clio_agent.gact.documents.anchors import ResolvedMark  # noqa: PLC0415

        where = f"cell {anchor.cell_range}" if anchor.cell_range else "a comment in the file itself"
        lines = [("In file", f"{where} of {target.name}")]
        if anchor.native_comment_id:
            lines.append(("Comment id", anchor.native_comment_id))
        return ResolvedMark(mode="native", lines=lines)

    def page_image(self, target: Any, page_index: int | None) -> None:
        return None

    def reanchor(self, old: Any, new: Any, anchor: Any) -> tuple[str, Any]:
        return "current", anchor


def register_builtin() -> None:
    """Native comments on every Office and OpenDocument profile."""

    from clio_agent.gact.documents.anchors import register  # noqa: PLC0415

    register(
        ("ooxml-word", "ooxml-sheet", "ooxml-slides", "odf-text", "odf-sheet", "odf-slides"),
        ("native-comment", "sheet-range"),
        NativeCommentResolver(),  # type: ignore[arg-type]
    )
