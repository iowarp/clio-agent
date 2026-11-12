"""PDF anchors: text quotes, selected quads and dragged regions on rendered pages.

Covers PDF artifacts and every Office file shown through its PDF rendition. Pages are
rasterized at two pixels per PDF point (``pdftoppm``) for the overview and crops, and the text
under a mark is read from the page's word boxes (``pdftotext -bbox``), both run inside the
write fence with the network denied. When a tool is missing, the entry says so.
"""

from __future__ import annotations

import html
import re
import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.gact.documents.anchors import (
    AnchorState,
    MarkTarget,
    ResolvedMark,
    register,
)
from clio_agent.gact.documents.comment_settings import context_chars
from clio_agent.gact.documents.confined_tools import run_confined
from clio_agent.gact.documents.mark_images import crop_png
from clio_agent.gact.documents.models import DocumentAnchor
from clio_agent.gact.documents.office_map import office_source_line
from clio_agent.gact.documents.office_pictures import picture_lines
from clio_agent.runtime.document_stack.process import DocumentError, find_native, scratch_root

if TYPE_CHECKING:
    from PIL import Image

__all__ = ["PdfWord", "box_label", "register_builtin"]

DPI = 144
_PAGE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">')
_WORD = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>'
)


@dataclass(frozen=True)
class PdfWord:
    """One word on a page, with its box normalized to the page."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float


def _workspace_root(target: MarkTarget) -> Path | None:
    workspace = target.app.state.workspaces.get(target.record.workspace_id)
    root = str(getattr(workspace, "root_path", "") or "") if workspace else ""
    return Path(root).resolve(strict=False) if root else None


class _Scratch:
    """A private folder in the workspace's document scratch, removed on exit."""

    def __init__(self, target: MarkTarget) -> None:
        root = _workspace_root(target)
        base = scratch_root(root) if root is not None else Path.cwd()
        self.path = base / "comments" / uuid.uuid4().hex
        self.workspace_root = root

    def __enter__(self) -> "_Scratch":
        self.path.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *_exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _page_count(target: MarkTarget) -> int:
    cached = target.cache.get("pdf_pages")
    if cached is None:
        from pypdf import PdfReader  # noqa: PLC0415 - optional heavy import

        cached = len(PdfReader(str(target.view_path)).pages)
        target.cache["pdf_pages"] = cached
    return int(cached)


def _page_words(target: MarkTarget, page_index: int) -> list[PdfWord] | None:
    key = f"pdf_words:{page_index}"
    if key in target.cache:
        return target.cache[key]
    tool = find_native("pdftotext")
    if tool is None:
        target.cache[key] = None
        return None
    with _Scratch(target) as scratch:
        out = scratch.path / "words.html"
        page = str(page_index + 1)
        try:
            run_confined(
                [tool, "-f", page, "-l", page, "-bbox", str(target.view_path), str(out)],
                cwd=scratch.path,
                workspace_root=scratch.workspace_root,
            )
            text = out.read_text(encoding="utf-8", errors="replace")
        except (DocumentError, OSError):
            target.cache[key] = None
            return None
    size = _PAGE.search(text)
    width, height = (float(size.group(1)), float(size.group(2))) if size else (1.0, 1.0)
    words = [
        PdfWord(
            html.unescape(match.group(5)),
            float(match.group(1)) / width,
            float(match.group(2)) / height,
            float(match.group(3)) / width,
            float(match.group(4)) / height,
        )
        for match in _WORD.finditer(text)
    ]
    target.cache[key] = words
    return words


def _page_raster(target: MarkTarget, page_index: int) -> "Image.Image | None":
    key = f"pdf_raster:{page_index}"
    if key in target.cache:
        return target.cache[key]
    tool = find_native("pdftoppm")
    image = None
    if tool is not None:
        from PIL import Image  # noqa: PLC0415 - optional heavy import

        with _Scratch(target) as scratch:
            page = str(page_index + 1)
            prefix = scratch.path / "page"
            try:
                run_confined(
                    [tool, "-f", page, "-l", page, "-r", str(DPI), "-png", "-singlefile"]
                    + [str(target.view_path), str(prefix)],
                    cwd=scratch.path,
                    workspace_root=scratch.workspace_root,
                )
                with Image.open(prefix.with_suffix(".png")) as opened:
                    image = opened.convert("RGB")
            except (DocumentError, OSError):
                image = None
    target.cache[key] = image
    return image


def _joined(words: Sequence[PdfWord]) -> tuple[str, list[int]]:
    """Words joined with spaces, and the word each character came from."""

    chars: list[str] = []
    owner: list[int] = []
    for index, word in enumerate(words):
        if chars:
            chars.append(" ")
            owner.append(index)
        chars.extend(word.text)
        owner.extend([index] * len(word.text))
    return "".join(chars), owner


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _union(words: Sequence[PdfWord]) -> tuple[float, float, float, float]:
    x0 = min(w.x0 for w in words)
    y0 = min(w.y0 for w in words)
    x1 = max(w.x1 for w in words)
    y1 = max(w.y1 for w in words)
    return x0, y0, max(1e-4, x1 - x0), max(1e-4, y1 - y0)


def _quads_rect(quads: Sequence[Sequence[float]]) -> tuple[float, float, float, float] | None:
    if not quads:
        return None
    xs = [q[i] for q in quads for i in range(0, 8, 2)]
    ys = [q[i] for q in quads for i in range(1, 8, 2)]
    return min(xs), min(ys), max(1e-4, max(xs) - min(xs)), max(1e-4, max(ys) - min(ys))


def _words_in(words: Sequence[PdfWord], rect: Sequence[float]) -> list[PdfWord]:
    x, y, width, height = rect
    inside = [
        word
        for word in words
        if x <= (word.x0 + word.x1) / 2 <= x + width and y <= (word.y0 + word.y1) / 2 <= y + height
    ]
    return sorted(inside, key=lambda word: (round(word.y0, 3), word.x0))


def box_label(rect: Sequence[float], where: str) -> str:
    """``x 8% to 52%, y 11% to 39% of <where>``."""

    x, y, width, height = rect
    return (
        f"x {round(x * 100)}% to {round((x + width) * 100)}%, "
        f"y {round(y * 100)}% to {round((y + height) * 100)}% of {where}"
    )


def _find_text(
    target: MarkTarget, exact: str, prefer_page: int | None
) -> tuple[int, int, int, list[PdfWord]] | None:
    """(page, start, end, words) where ``exact`` sits, preferring ``prefer_page``."""

    needle = _collapse(exact)
    if not needle:
        return None
    pages = list(range(_page_count(target)))
    if prefer_page is not None and prefer_page in pages:
        pages.remove(prefer_page)
        pages.insert(0, prefer_page)
    for page in pages:
        words = _page_words(target, page)
        if not words:
            continue
        text, _owner = _joined(words)
        start = text.find(needle)
        if start != -1:
            return page, start, start + len(needle), words
    return None


class PdfResolver:
    """Resolve PDF and rendition anchors; text moves with its text, regions stay put."""

    def _source(
        self, target: MarkTarget, page: int | None, rect: Sequence[float] | None, text: str
    ) -> tuple[str | None, str | None]:
        if not target.via_rendition:
            return None, None
        return office_source_line(
            target.profile, target.path, page_index=page, rect=rect, text=text
        )

    def resolve(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        if anchor.profile == "region":
            return self._region(target, anchor)
        return self._text(target, anchor)

    def _text(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        mark = ResolvedMark(mode="text")
        exact = _collapse(anchor.exact)
        found = _find_text(target, exact, anchor.page_index)
        page = anchor.page_index
        rect = _quads_rect(anchor.quads)
        lines: list[tuple[str, str]] = [("Selected", f'"{exact}"')]
        if found is not None:
            page, start, end, words = found
            text, owner = _joined(words)
            width = context_chars()
            before, after = text[max(0, start - width) : start], text[end : end + width]
            if before:
                lines.append(("Before", f'"{"..." if start > width else ""}{before}"'))
            if after:
                lines.append(("After", f'"{after}{"..." if end + width < len(text) else ""}"'))
            if rect is None:
                rect = _union([words[i] for i in sorted(set(owner[start:end]))])
        elif exact:
            mark.notes.append("quote_not_found: the selected text is not on this version's pages")
        if page is not None:
            lines.append(("Page", str(page + 1)))
        source, note = self._source(target, page, rect, exact)
        if source:
            lines.append(("Source", source))
        if note:
            mark.notes.append(note)
        mark.lines, mark.under, mark.page_index, mark.rect = lines, exact, page, rect
        return mark

    def _region(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        page = anchor.page_index if anchor.page_index is not None else 0
        rect = tuple(anchor.rect)
        mark = ResolvedMark(mode="region", page_index=page, rect=rect)  # type: ignore[arg-type]
        mark.lines.append(("Box", box_label(rect, f"page {page + 1}")))
        words = _page_words(target, page)
        if words is None:
            mark.notes.append("page_text_unavailable: pdftotext is not installed or failed")
            under = ""
        else:
            under = " ".join(word.text for word in _words_in(words, rect))
        mark.under = under
        if under:
            mark.lines.append(("Under", f'"{under}"'))
        source, note = self._source(target, page, rect, under)
        if source:
            mark.lines.append(("Source", source))
        if note:
            mark.notes.append(note)
        if target.via_rendition:
            pictures, picture_notes = picture_lines(target, page, rect)
            mark.lines.extend(pictures)
            mark.notes.extend(picture_notes)
        image = _page_raster(target, page)
        if image is None:
            mark.notes.append("page_image_unavailable: pdftoppm is not installed or failed")
        else:
            mark.crop = crop_png(image, rect)
        return mark

    def page_image(self, target: MarkTarget, page_index: int | None) -> "Image.Image | None":
        return _page_raster(target, page_index or 0)

    def reanchor(
        self, old: MarkTarget, new: MarkTarget, anchor: DocumentAnchor
    ) -> tuple[AnchorState, DocumentAnchor]:
        if anchor.profile == "region":
            return "current", anchor
        found = _find_text(new, anchor.exact, anchor.page_index)
        if found is None:
            return "outdated", anchor
        page, start, end, words = found
        text, owner = _joined(words)
        picked = [words[i] for i in sorted(set(owner[start:end]))]
        quads = [[w.x0, w.y0, w.x1, w.y0, w.x1, w.y1, w.x0, w.y1] for w in picked]
        return "moved", anchor.model_copy(update={"page_index": page, "quads": quads})


def register_builtin() -> None:
    """Text and regions on PDFs and on every Office file's PDF rendition."""

    register(
        (
            "pdf",
            "ooxml-word",
            "ooxml-sheet",
            "ooxml-slides",
            "odf-text",
            "odf-sheet",
            "odf-slides",
        ),
        ("text-quote", "pdf-quad", "region"),
        PdfResolver(),
    )
