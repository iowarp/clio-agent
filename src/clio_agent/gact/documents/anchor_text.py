"""Text quotes on Markdown, LaTeX, plain text and HTML: find them in the source again.

The person selects rendered text (a Markdown preview, an HTML page), so the quote is matched
against the source's rendered text: Markdown markup and HTML tags are dropped and whitespace
is collapsed, with a map back to source offsets so the entry can name the source lines. The
quote's exact text, the text before and after it, and its offset in the rendered text pick
one occurrence when the text appears more than once.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Literal

from clio_agent.gact.documents.anchors import (
    AnchorState,
    MarkTarget,
    ResolvedMark,
    register,
)
from clio_agent.gact.documents.comment_settings import context_chars
from clio_agent.gact.documents.models import DocumentAnchor

__all__ = ["TextMap", "find_quote", "quote_lines", "register_builtin", "text_map"]

TextKind = Literal["markdown", "html", "plain"]

_MD_DROP = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]+"  # heading markers
    r"|^[ \t]*>[ \t]?"  # block quote markers
    r"|^[ \t]*(?:[-*+]|\d+[.)])[ \t]+"  # list markers
    r"|\*\*|__|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)"  # emphasis
    r"|`+",  # code spans
    re.MULTILINE,
)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_SKIP = re.compile(r"(?is)<(script|style|template)\b.*?</\1\s*>|<!--.*?-->|<[^>]+>")
_HTML_ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


@dataclass(frozen=True)
class TextMap:
    """Rendered text with whitespace collapsed, and each character's offset in the source."""

    text: str
    offsets: list[int]
    source: str

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """The source offsets of rendered characters ``start`` to ``end``."""
        last = max(start, end - 1)
        return self.offsets[start], self.offsets[last] + 1

    def source_lines(self, start: int, end: int) -> tuple[int, int]:
        """1-based first and last source lines of rendered characters ``start`` to ``end``."""
        first, stop = self.source_span(start, end)
        return self.source.count("\n", 0, first) + 1, self.source.count("\n", 0, stop - 1) + 1


def _kept_spans(source: str, kind: TextKind) -> list[tuple[int, str]]:
    """(source offset, text) pieces that survive rendering, in order."""

    if kind == "plain":
        return [(0, source)]
    pieces: list[tuple[int, str]] = []
    if kind == "html":
        cursor = 0
        for match in _HTML_SKIP.finditer(source):
            pieces.append((cursor, source[cursor : match.start()]))
            pieces.append((match.start(), " "))
            cursor = match.end()
        pieces.append((cursor, source[cursor:]))
        decoded: list[tuple[int, str]] = []
        for offset, text in pieces:
            pos = 0
            for match in _HTML_ENTITY.finditer(text):
                decoded.append((offset + pos, text[pos : match.start()]))
                decoded.append((offset + match.start(), html.unescape(match.group(0))))
                pos = match.end()
            decoded.append((offset + pos, text[pos:]))
        return decoded
    cursor = 0
    links: list[tuple[int, str]] = []
    for match in _MD_LINK.finditer(source):
        links.append((cursor, source[cursor : match.start()]))
        links.append((match.start(1), match.group(1)))
        cursor = match.end()
    links.append((cursor, source[cursor:]))
    for offset, text in links:
        pos = 0
        for match in _MD_DROP.finditer(text):
            pieces.append((offset + pos, text[pos : match.start()]))
            pos = match.end()
        pieces.append((offset + pos, text[pos:]))
    return pieces


def text_map(source: str, kind: TextKind) -> TextMap:
    """The rendered text of ``source`` with whitespace runs collapsed to one space."""

    chars: list[str] = []
    offsets: list[int] = []
    for offset, text in _kept_spans(source, kind):
        for index, char in enumerate(text):
            if char.isspace():
                if not chars or chars[-1] == " ":
                    continue
                char = " "
            chars.append(char)
            offsets.append(offset + index)
    while chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()
    return TextMap("".join(chars), offsets, source)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _agreement(text: str, start: int, end: int, prefix: str, suffix: str) -> int:
    """How many characters of ``prefix`` and ``suffix`` agree with the text around a hit."""

    score = 0
    before = text[:start]
    for a, b in zip(reversed(prefix), reversed(before), strict=False):
        if a != b:
            break
        score += 1
    after = text[end:]
    for a, b in zip(suffix, after, strict=False):
        if a != b:
            break
        score += 1
    return score


def find_quote(
    mapped: TextMap, exact: str, prefix: str = "", suffix: str = "", near: int | None = None
) -> tuple[int, int] | None:
    """Where ``exact`` sits in the rendered text, or ``None`` when it is not there.

    With several occurrences, the one whose surroundings agree most with ``prefix`` and
    ``suffix`` wins, then the one nearest ``near`` (the selection's rendered offset).
    """

    needle = _collapse(exact)
    if not needle:
        return None
    prefix, suffix = _collapse(prefix), _collapse(suffix)
    hits: list[tuple[int, int, int]] = []
    start = mapped.text.find(needle)
    while start != -1:
        end = start + len(needle)
        score = _agreement(mapped.text, start, end, prefix.rstrip(), suffix.lstrip())
        score += _agreement(mapped.text, start, end, prefix, suffix)
        distance = abs(start - near) if near is not None else 0
        hits.append((-score, distance, start))
        start = mapped.text.find(needle, start + 1)
    if not hits:
        return None
    _score, _distance, best = min(hits)
    return best, best + len(needle)


def quote_lines(
    mapped: TextMap, start: int, end: int, *, source_name: str
) -> list[tuple[str, str]]:
    """The Selected, Before, After and Source lines for a quote found at ``start``..``end``."""

    width = context_chars()
    selected = mapped.text[start:end]
    before = mapped.text[max(0, start - width) : start]
    after = mapped.text[end : end + width]
    first, last = mapped.source_lines(start, end)
    where = f"line {first}" if first == last else f"lines {first} to {last}"
    lines = [("Selected", f'"{selected}"')]
    if before:
        lines.append(("Before", f'"{"..." if start > width else ""}{before}"'))
    if after:
        lines.append(("After", f'"{after}{"..." if end + width < len(mapped.text) else ""}"'))
    lines.append(("Source", f"{where} of {source_name}"))
    return lines


def _kind_for(target: MarkTarget) -> TextKind:
    if target.profile == "markdown":
        return "markdown"
    if target.profile == "html-static":
        return "html"
    return "plain"


def _mapped(target: MarkTarget) -> TextMap:
    cached = target.cache.get("text_map")
    if cached is None:
        source = target.path.read_bytes().decode("utf-8", errors="replace")
        cached = text_map(source, _kind_for(target))
        target.cache["text_map"] = cached
    return cached


class TextResolver:
    """Resolve and re-anchor ``text-quote`` anchors on text sources."""

    def resolve(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        mapped = _mapped(target)
        found = find_quote(mapped, anchor.exact, anchor.prefix, anchor.suffix, anchor.start)
        if found is None:
            selected = _collapse(anchor.exact)
            return ResolvedMark(
                mode="text",
                lines=[("Selected", f'"{selected}"')],
                under=selected,
                notes=["quote_not_found: the selected text is not in this version's source"],
            )
        start, end = found
        return ResolvedMark(
            mode="text",
            lines=quote_lines(mapped, start, end, source_name=target.name),
            under=mapped.text[start:end],
        )

    def page_image(self, target: MarkTarget, page_index: int | None) -> None:
        return None

    def reanchor(
        self, old: MarkTarget, new: MarkTarget, anchor: DocumentAnchor
    ) -> tuple[AnchorState, DocumentAnchor]:
        mapped = _mapped(new)
        found = find_quote(mapped, anchor.exact, anchor.prefix, anchor.suffix, anchor.start)
        if found is None:
            return "outdated", anchor
        start, end = found
        width = context_chars()
        return "moved", anchor.model_copy(
            update={
                "start": start,
                "end": end,
                "prefix": mapped.text[max(0, start - width) : start],
                "suffix": mapped.text[end : end + width],
            }
        )


def register_builtin() -> None:
    """Text quotes on every text source the document surface shows."""

    register(("markdown", "latex", "html-static", "binary"), ("text-quote",), TextResolver())
