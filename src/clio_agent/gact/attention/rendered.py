"""Map text a person selected in the rendered transcript back onto its markdown source.

A browser selection is the *rendered* text: emphasis markers, list bullets,
heading hashes, table pipes, code backticks and link targets are gone, and
whitespace is reflowed. The attention request needs a span of the *source*
text (what the model generated). Both sides are projected the same way --
markup characters dropped, whitespace runs collapsed to one space -- and the
projection keeps, per character, its source offset, so a match converts back to
an exact source span. This is a format transform with one fixed rule set, not a
fuzzy search: a selection that does not project onto the source is not found.
"""

from __future__ import annotations

import re

#: Characters markdown renders away (emphasis, code, headings, quotes, tables).
_MARKUP = frozenset("*_`#>|~")
#: Line-leading list markers (``- item``, ``1. item``), rendered as bullets/numbers.
_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
#: The ``(target)`` half of a markdown link or image; only the label renders.
_LINK_TARGET = re.compile(r"\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")


def _project(text: str, *, source: bool) -> tuple[str, list[int]]:
    """``text`` with markup dropped and whitespace collapsed, plus source offsets."""
    keep = [True] * len(text)
    if source:
        for pattern in (_LIST_MARKER, _LINK_TARGET):
            for match in pattern.finditer(text):
                lo, hi = match.span()
                if pattern is _LINK_TARGET:
                    lo += 1  # keep nothing of "(target)" but the "]" is markup too
                    keep[lo - 1] = False
                for i in range(lo, hi):
                    keep[i] = False
    out: list[str] = []
    index: list[int] = []
    pending_space = False
    for i, ch in enumerate(text):
        if not keep[i] or ch in _MARKUP or ch == "[" or ch == "]":
            continue
        if ch.isspace():
            pending_space = bool(out)
            continue
        if pending_space:
            out.append(" ")
            index.append(i)
            pending_space = False
        out.append(ch)
        index.append(i)
    return "".join(out), index


def find_rendered(source: str, selected: str) -> tuple[int, int] | None:
    """The source span ``[start, end)`` whose rendering is ``selected``, or ``None``."""
    needle, _ = _project(selected, source=False)
    if not needle:
        return None
    haystack, index = _project(source, source=True)
    found = haystack.find(needle)
    if found < 0:
        return None
    return index[found], index[found + len(needle) - 1] + 1
