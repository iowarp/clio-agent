"""A SyncTeX index: PDF positions to source lines and back (LaTeX plan, "SyncTeX"; ledger L3, L7).

Tectonic writes ``<stem>.synctex.gz`` with ``--synctex``: gzip-compressed lines. ``Input:<tag>:
<path>`` names a source file (also part-way through the content). ``{<page>`` and ``}<page>``
bound a page. Each box record is ``<kind><tag>,<line>:<x>,<y>[:<w>,<h>,<d>]`` in scaled points.
The kinds are ``[`` (vbox), ``(`` (hbox), ``h`` (void hbox), ``v`` (void vbox), ``k`` (kern),
``g`` (glue), ``x`` (current point) and ``$`` (math). Positions are measured from the page's
top-left corner. ``y`` is the baseline, so a box spans ``y - h`` to ``y + d``. One big point
(1/72 inch) is 65781.76 scaled points (checked against ``pdftotext -bbox`` on a Tectonic build:
a paragraph's indent box ends exactly where its first word begins).

Input paths are the build directory's absolute paths. The index maps them to project-relative
paths through the build's source list, so it stays valid after the build directory is gone.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

__all__ = ["SP_PER_BP", "Box", "SyncIndex"]

SP_PER_BP = 65781.76
_RECORD = re.compile(
    r"^(?P<kind>[\[\(hvkgx$])(?P<tag>\d+),(?P<line>\d+):(?P<x>-?\d+),(?P<y>-?\d+)"
    r"(?::(?P<w>-?\d+)(?:,(?P<h>-?\d+),(?P<d>-?\d+))?)?"
)
_INPUT = re.compile(r"^Input:(?P<tag>\d+):(?P<path>.*)$")
_PAGE = re.compile(r"^\{(?P<page>\d+)$")
# Records inside a line of text. TeX gives a paragraph's line boxes the line where the
# paragraph ended, but the glue, kern and point records in a line carry the line each word
# came from, so those decide; a small ``(`` box (an inline box) counts too.
_WORD_KINDS = frozenset("hkgx$")
_INLINE_BOX_MAX_HEIGHT = 24.0
# A text row's reach around its baseline, in big points: ascenders above, descenders below.
_ASCENT, _DESCENT = 10.0, 3.0


@dataclass(frozen=True)
class Box:
    """One SyncTeX record on a page: a source line and its box in big points from top-left."""

    page: int  # 0-based
    file: str
    line: int
    kind: str
    x: float
    top: float
    width: float
    height: float
    baseline: float = 0.0

    @property
    def word_level(self) -> bool:
        """Whether this record sits inside a line of text (not a paragraph or column)."""
        return self.kind in _WORD_KINDS or (
            self.kind == "(" and self.height <= _INLINE_BOX_MAX_HEIGHT
        )

    def extent(self) -> tuple[float, float, float, float]:
        """``x0, y0, x1, y1`` of the record, giving a zero-height point its text row."""
        if self.height > 0:
            return self.x, self.top, self.x + self.width, self.top + self.height
        return self.x, self.baseline - _ASCENT, self.x + self.width, self.baseline + _DESCENT


def _relative(path: str, sources: Iterable[str]) -> str:
    """The project-relative name of an absolute build path, by the longest matching source."""
    clean = path.replace("\\", "/")
    best = ""
    for name in sources:
        if clean == name or clean.endswith("/" + name):
            if len(name) > len(best):
                best = name
    if best:
        return best
    # A source read through \input without its .tex is still the .tex file.
    for name in sources:
        if name.endswith(".tex") and (clean + ".tex").endswith("/" + name):
            return name
    return ""


class SyncIndex:
    """The boxes of a build's pages, by page and by source file."""

    def __init__(self, boxes: list[Box]) -> None:
        self.boxes = boxes
        self.pages = sorted({box.page for box in boxes})

    @classmethod
    def parse(cls, data: bytes, sources: Iterable[str]) -> SyncIndex:
        """Read a ``.synctex.gz`` (or its uncompressed text) for a build of ``sources``."""
        known = list(sources)
        text = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
        tags: dict[str, str] = {}
        boxes: list[Box] = []
        page = -1
        content = False
        for raw in text.decode("utf-8", errors="replace").splitlines():
            if raw.startswith("Input:"):
                found = _INPUT.match(raw)
                if found:
                    tags[found.group("tag")] = _relative(found.group("path"), known)
                continue
            if raw.startswith("Content:"):
                content = True
                continue
            if not content:
                continue
            if raw.startswith("{"):
                started = _PAGE.match(raw)
                page = int(started.group("page")) - 1 if started else page
                continue
            if raw.startswith("Postamble:"):
                break
            record = _RECORD.match(raw)
            if record is None or page < 0:
                continue
            file = tags.get(record.group("tag"), "")
            if not file:
                continue  # a class or package file from the bundle, not the project
            width = int(record.group("w") or 0) / SP_PER_BP
            height = int(record.group("h") or 0) / SP_PER_BP
            depth = int(record.group("d") or 0) / SP_PER_BP
            y = int(record.group("y")) / SP_PER_BP
            boxes.append(
                Box(
                    page=page,
                    file=file,
                    line=int(record.group("line")),
                    kind=record.group("kind"),
                    x=int(record.group("x")) / SP_PER_BP,
                    top=y - height,
                    width=max(width, 0.0),
                    height=max(height + depth, 0.0),
                    baseline=y,
                )
            )
        return cls(boxes)

    def _words(self, page: int) -> list[Box]:
        return [box for box in self.boxes if box.page == page and box.word_level]

    def source_at(self, page: int, x: float, y: float) -> tuple[str, int] | None:
        """The source file and line at a point of ``page`` (big points from top-left).

        The text row whose baseline lies just below the point is taken, and in it the last
        word-level record at or before the point. Away from any text, the nearest record on
        the page is taken, so a click in a margin still lands on a line.
        """
        words = self._words(page)
        if not words:
            return None
        near = [box for box in words if -_DESCENT - 1 <= box.baseline - y <= _ASCENT + 4]
        if near:
            row = min(near, key=lambda box: abs(box.baseline - (y + _DESCENT))).baseline
            in_row = [box for box in near if abs(box.baseline - row) < 0.5]
            before = [box for box in in_row if box.x <= x + 0.5]
            pick = max(before, key=lambda box: box.x) if before else min(in_row, key=lambda b: b.x)
            return pick.file, pick.line

        def distance(box: Box) -> float:
            x0, y0, x1, y1 = box.extent()
            dx = max(x0 - x, 0.0, x - x1)
            dy = max(y0 - y, 0.0, y - y1)
            return dx * dx + dy * dy

        pick = min(words, key=distance)
        return pick.file, pick.line

    def lines_in(
        self, page: int, x0: float, y0: float, x1: float, y1: float
    ) -> dict[str, tuple[int, int]]:
        """The source line range per file of the text inside a rectangle of ``page``."""
        ranges: dict[str, tuple[int, int]] = {}
        for box in self._words(page):
            bx0, by0, bx1, by1 = box.extent()
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            low, high = ranges.get(box.file, (box.line, box.line))
            ranges[box.file] = (min(low, box.line), max(high, box.line))
        return ranges

    def places(
        self, file: str, start: int, end: int
    ) -> list[tuple[int, tuple[float, float, float, float]]]:
        """Where lines ``start`` to ``end`` of ``file`` land: per page, the union of their text
        as ``(x, top, width, height)`` in big points."""
        by_page: dict[int, list[tuple[float, float, float, float]]] = {}
        for box in self.boxes:
            if box.file == file and start <= box.line <= end and box.word_level:
                by_page.setdefault(box.page, []).append(box.extent())
        places = []
        for page, extents in sorted(by_page.items()):
            x0 = min(e[0] for e in extents)
            y0 = min(e[1] for e in extents)
            x1 = max(e[2] for e in extents)
            y1 = max(e[3] for e in extents)
            places.append((page, (x0, y0, x1 - x0, y1 - y0)))
        return places

    def files(self) -> list[str]:
        """The project files the PDF has boxes from."""
        return sorted({box.file for box in self.boxes}, key=lambda name: PurePosixPath(name).parts)
