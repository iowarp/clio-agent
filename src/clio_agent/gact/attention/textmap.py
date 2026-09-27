"""Locate transcript text inside model-facing text, keeping a char map.

Text reaches a prompt (or leaves a model) either verbatim or JSON-string
escaped -- a tool result inside ``{"tool_call_results": [...]}``, a final answer
inside ``submit``'s JSON args. Locating is exact string equality in one of
those two encodings (a format transform, no fuzzy matching), and the result
carries a per-character map so a span in one text converts exactly to a span
in the other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Located:
    """``needle`` found in a haystack; ``index[i]`` = haystack offset of needle char ``i``."""

    start: int
    index: list[int]
    encoding: str

    @property
    def end(self) -> int:
        """Haystack offset just past the needle."""
        return self.index[-1]

    def to_haystack(self, lo: int, hi: int) -> tuple[int, int]:
        """Needle span ``[lo, hi)`` as a haystack span."""
        lo = max(0, min(lo, len(self.index) - 1))
        hi = max(lo, min(hi, len(self.index) - 1))
        return self.index[lo], self.index[hi]

    def from_haystack(self, lo: int, hi: int) -> tuple[int, int] | None:
        """Haystack span ``[lo, hi)`` clipped to the needle, as a needle span."""
        if hi <= self.start or lo >= self.end:
            return None
        from bisect import bisect_left, bisect_right  # noqa: PLC0415

        n_lo = max(0, bisect_right(self.index, lo) - 1)
        n_hi = min(len(self.index) - 1, bisect_left(self.index, hi))
        return (n_lo, n_hi) if n_hi > n_lo else None


def _escaped(needle: str, ensure_ascii: bool) -> tuple[str, list[int]]:
    pieces: list[str] = []
    index = [0]
    for ch in needle:
        esc = json.dumps(ch, ensure_ascii=ensure_ascii)[1:-1]
        pieces.append(esc)
        index.append(index[-1] + len(esc))
    return "".join(pieces), index


def locate(haystack: str, needle: str, start: int = 0) -> Located | None:
    """First occurrence of ``needle`` at or after ``start``: verbatim, then JSON-escaped."""
    if not needle:
        return None
    found = haystack.find(needle, start)
    if found >= 0:
        return Located(found, [found + i for i in range(len(needle) + 1)], "verbatim")
    for ensure_ascii in (False, True):
        escaped, index = _escaped(needle, ensure_ascii)
        if escaped == needle:
            continue
        found = haystack.find(escaped, start)
        if found >= 0:
            return Located(found, [found + i for i in index], "json")
    return None


def locate_all(haystack: str, needle: str) -> list[Located]:
    """Every non-overlapping occurrence (both encodings), in haystack order."""
    out: list[Located] = []
    cursor = 0
    while True:
        hit = locate(haystack, needle, cursor)
        if hit is None:
            return out
        out.append(hit)
        cursor = hit.end
