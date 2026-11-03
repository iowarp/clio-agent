"""Paged reads and a streaming search over a resource's text: all of it is reachable.

A text representation (an uploaded text original or a textual derivative) is decoded
as a stream, so neither operation holds the whole file. A read skips to a character
offset and returns ``limit`` characters; a search walks the text line by line, skips
``offset`` earlier matches and returns ``limit`` more. The per-call defaults are page
sizes, not walls: every result names the offset that continues it. A line longer than
:data:`PIECE_CHARS` is scanned in pieces (the memory unit of the scan, with an overlap
so a match across a piece boundary is found), so a file without newlines is searched
without being held whole.

Offsets count characters of the text exactly as stored (``newline=""``: a ``\\r\\n``
pair is two characters), the same in both operations, so a match's ``char_offset``
can be passed straight to a read. Decoding errors propagate as
:class:`UnicodeDecodeError` for the owner (:mod:`clio_agent.gact.resource_tools`) to
type.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

__all__ = ["PIECE_CHARS", "TextPage", "read_text_page", "search_text"]

#: Characters decoded per step while skipping or scanning (memory, not visibility).
PIECE_CHARS = 1 << 20


@dataclass(frozen=True)
class TextPage:
    """One page of text and where the text continues."""

    content: str
    next_offset: int | None
    total_chars: int | None


def _skip(stream: IO[str], count: int | None) -> int:
    """Consume up to ``count`` characters (all when ``None``); return how many."""

    skipped = 0
    while count is None or skipped < count:
        step = PIECE_CHARS if count is None else min(PIECE_CHARS, count - skipped)
        piece = stream.read(step)
        if not piece:
            break
        skipped += len(piece)
    return skipped


def read_text_page(path: Path, *, offset: int, limit: int, count_total: bool) -> TextPage:
    """Read ``limit`` characters from character ``offset``.

    Args:
        path: The text representation.
        offset: Zero-based character offset to start at.
        limit: Characters to return.
        count_total: Scan to the end so ``total_chars`` is known even when the page
            stops early (the owner decides when that scan is worth its time).
    """

    with path.open(encoding="utf-8", newline="") as stream:
        skipped = _skip(stream, offset)
        if skipped < offset:
            return TextPage(content="", next_offset=None, total_chars=skipped)
        content = stream.read(limit)
        end = offset + len(content)
        if len(content) < limit:
            return TextPage(content=content, next_offset=None, total_chars=end)
        if count_total:
            rest = _skip(stream, None)
            return TextPage(content, end if rest else None, end + rest)
        more = stream.read(1)
        return TextPage(content, end if more else None, None if more else end)


def _excerpt(window: str, at: int, chars: int) -> tuple[int, str]:
    """The excerpt of ``window`` around a match at ``at``: its start and its text."""

    if len(window) <= chars:
        return 0, window
    start = max(0, min(min(at, len(window)) - chars // 4, len(window) - chars))
    return start, window[start : start + chars]


def search_text(
    path: Path, needle: str, *, offset: int, limit: int, excerpt_chars: int
) -> tuple[list[dict[str, Any]], int | None]:
    """Case-insensitive line search: matches ``offset+1..offset+limit`` and the next offset.

    Each matching line is one match: its 1-based ``line``, an excerpt ``text`` of at
    most ``excerpt_chars`` characters around the first hit, and the excerpt's
    ``char_offset`` in the text. A line longer than its excerpt also reports
    ``line_chars`` so the rest can be read from ``char_offset``.

    Args:
        path: The text representation.
        needle: The casefolded, non-empty query.
        offset: Matches to skip (a previous result's ``next_offset``).
        limit: Matches to return.
        excerpt_chars: Characters of each excerpt.
    """

    matches: list[dict[str, Any]] = []
    overlap = len(needle) - 1
    found = 0
    line_number, position, line_chars = 1, 0, 0
    tail = ""
    line_matched = False
    current: dict[str, Any] | None = None
    with path.open(encoding="utf-8", newline="") as stream:
        while piece := stream.readline(PIECE_CHARS):
            ends = piece.endswith(("\n", "\r"))
            body = piece.rstrip("\r\n") if ends else piece
            window = tail + body
            if not line_matched:
                at = window.casefold().find(needle)
                if at >= 0:
                    line_matched = True
                    found += 1
                    if found > offset:
                        if len(matches) == limit:
                            return matches, offset + limit
                        start, text = _excerpt(window, at, excerpt_chars)
                        current = {
                            "line": line_number,
                            "char_offset": position - len(tail) + start,
                            "text": text,
                        }
                        matches.append(current)
            line_chars += len(body)
            position += len(piece)
            tail = window[-overlap:] if overlap > 0 else ""
            if ends:
                if current is not None and line_chars > len(current["text"]):
                    current["line_chars"] = line_chars
                line_number, line_chars, tail = line_number + 1, 0, ""
                line_matched, current = False, None
    if current is not None and line_chars > len(current["text"]):
        current["line_chars"] = line_chars
    return matches, None
