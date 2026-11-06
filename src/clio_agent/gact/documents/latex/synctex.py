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
from collections.abc import Iterable, Sequence
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
#: Records that carry their own word's line, never the paragraph's end line.
_TEXT_KINDS = frozenset("gx$h")
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


def _pieces(boxes: Sequence[Box]) -> list[list[Box]]:
    """A page's word records as text rows, each split into its columns' line boxes.

    A record goes with the widest hbox on its baseline that contains it (a column's line); a
    row without hboxes is one piece.
    """
    rows: dict[int, list[Box]] = {}
    for box in boxes:
        rows.setdefault(round(box.baseline * 2), []).append(box)
    pieces: list[list[Box]] = []
    for row in rows.values():
        lines = sorted((box for box in row if box.kind == "("), key=lambda box: -box.width)
        groups: dict[int, list[Box]] = {}
        loose: list[Box] = []
        for box in row:
            home = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if line.x - 0.5 <= box.x <= line.x + line.width + 0.5
                ),
                None,
            )
            if home is None:
                loose.append(box)
            else:
                groups.setdefault(home, []).append(box)
        pieces.extend(groups.values())
        if loose:
            pieces.append(loose)
    return pieces


def _line_ends(piece: Sequence[Box]) -> list[float]:
    return [line.x + line.width for line in piece if line.kind == "("]


def _ends_a_line(piece: Sequence[Box]) -> bool:
    """Whether the piece has the kern or glue TeX puts at a paragraph line's right end."""
    return any(_at_line_end(box, _line_ends(piece)) for box in piece)


def _at_line_end(box: Box, ends: Sequence[float]) -> bool:
    return box.kind in "kg" and any(abs(box.x - end) < 0.5 for end in ends)


def _words(piece: Sequence[Box]) -> list[Box]:
    """The records of a row piece that carry their own word's line.

    Not the line's hbox, nor the kern and glue TeX puts at the line's right end: they carry
    the line where the paragraph ended.
    """
    ends = _line_ends(piece)
    return [
        box
        for box in piece
        if (box.kind in _TEXT_KINDS or box.kind == "k") and not _at_line_end(box, ends)
    ]


def _span(box: Box) -> tuple[float, float]:
    """A record's x range (a kern or glue record is a point)."""
    if box.kind in "kg":
        return box.x, box.x
    return box.x, box.x + box.width


def _columns(
    extents: Sequence[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """The union of ``x0, y0, x1, y1`` extents per column: extents whose x ranges overlap."""
    merged: list[list[float]] = []
    for x0, y0, x1, y1 in sorted(extents):
        last = merged[-1] if merged else None
        if last is not None and x0 <= last[2]:
            last[1], last[2], last[3] = min(last[1], y0), max(last[2], x1), max(last[3], y1)
        else:
            merged.append([x0, y0, x1, y1])
    return [(a, b, c, d) for a, b, c, d in merged]


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

    def _text(self, page: int) -> list[Box]:
        """The page's records that carry their own word's line, not a paragraph's end line."""
        found: list[Box] = []
        for piece in _pieces(self._words(page)):
            words = _words(piece)
            if not words:
                if _ends_a_line(piece):
                    continue
                words = list(piece)
            found.extend(words)
        return found

    def source_at(self, page: int, x: float, y: float) -> tuple[str, int] | None:
        """The source file and line at a point of ``page`` (big points from top-left).

        The text row whose baseline lies just below the point is taken, and in it the last
        word-level record at or before the point. Away from any text, the nearest record on
        the page is taken, so a click in a margin still lands on a line.
        """
        words = self._text(page) or self._words(page)
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
        """The source line range per file of the text inside a rectangle of ``page``.

        A record counts when its baseline lies in the rectangle (a selection's box reaches a
        little below its baseline), so the rows above and below do not leak in.
        """
        ranges: dict[str, tuple[int, int]] = {}
        for box in self._text(page):
            bx0, _by0, bx1, _by1 = box.extent()
            if bx1 < x0 or bx0 > x1 or not y0 <= box.baseline <= y1 + _DESCENT:
                continue
            low, high = ranges.get(box.file, (box.line, box.line))
            ranges[box.file] = (min(low, box.line), max(high, box.line))
        return ranges

    def places(
        self, file: str, start: int, end: int
    ) -> list[tuple[int, tuple[float, float, float, float]]]:
        """Where lines ``start`` to ``end`` of ``file`` land: per page and column, the union of
        their text as ``(x, top, width, height)`` in big points.

        Words decide: glue, kern and character records carry their word's line, while a
        paragraph's line boxes and the kern ending each line carry the line where the paragraph
        ended, which can be the next section's first line. A row whose words are all in range
        counts whole; a row that also holds other lines' words counts only the words in range.
        """
        by_page: dict[int, list[Box]] = {}
        for box in self.boxes:
            if box.word_level:
                by_page.setdefault(box.page, []).append(box)
        places = []
        for page in sorted(by_page):
            extents = []
            for piece in _pieces(by_page[page]):
                words = _words(piece)
                if not words:
                    if _ends_a_line(piece):
                        continue  # a paragraph row whose words left no record of their line
                    words = list(piece)  # a heading or other box of its own
                hits = [box for box in words if box.file == file and start <= box.line <= end]
                if not hits:
                    continue
                chosen = piece if len(hits) == len(words) else hits
                extents.append(
                    (
                        min(_span(box)[0] for box in chosen),
                        min(box.extent()[1] for box in chosen),
                        max(_span(box)[1] for box in chosen),
                        max(box.extent()[3] for box in chosen),
                    )
                )
            extents.extend(
                graphic.extent()
                for graphic in self._graphics(page, by_page[page], file, start, end)
            )
            for x0, y0, x1, y1 in _columns(extents):
                places.append((page, (x0, y0, x1 - x0, y1 - y0)))
        return places

    def _graphics(
        self, page: int, words: Sequence[Box], file: str, start: int, end: int
    ) -> list[Box]:
        """Boxes of lines ``start``..``end`` taller than a text line that hold no text rows.

        An ``\\includegraphics`` is one such hbox: its only records sit on its bottom edge, so
        the rows alone would place a figure at its caption and a strip under the picture.
        """

        def holds_text(box: Box) -> bool:
            bottom = box.top + box.height
            return any(
                box.x <= word.x <= box.x + box.width and box.top + 1 < word.baseline < bottom - 4
                for word in words
            )

        return [
            box
            for box in self.boxes
            if box.page == page
            and box.kind == "("
            and box.height > _INLINE_BOX_MAX_HEIGHT
            and box.file == file
            and start <= box.line <= end
            and not holds_text(box)
        ]

    def files(self) -> list[str]:
        """The project files the PDF has boxes from."""
        return sorted({box.file for box in self.boxes}, key=lambda name: PurePosixPath(name).parts)
