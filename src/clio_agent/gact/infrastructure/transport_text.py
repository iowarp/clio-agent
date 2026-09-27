"""Read machine output that crossed the Desktop's SSH terminal intact.

The Desktop runs every remote command inside one interactive OpenSSH session
on a pseudo-terminal. On Windows that terminal is ConPTY at 100 columns: it
re-renders what the remote prints, so a long line comes back hard-wrapped at
the column width, tabs become spaces, and terminal escape sequences may be
interleaved. Output parsed by CLIO (probe lines, ``inspect`` JSON) must
therefore never depend on line length, tabs or a clean byte stream.

Observed through the Desktop on ares (2026-09-27), a wrapped ``podman
inspect`` line arrives as ``...,"2",\r\n\x1b[29;100H,"--ctx-size",...``: at each
wrap ConPTY emits CRLF, moves the cursor back to the last column, and redraws
the character that was already there -- so the character after that cursor
move is a duplicate and is dropped along with the wrap.

Format-only correction: nothing here changes what the remote said.
"""

from __future__ import annotations

import re

#: A ConPTY soft wrap: CRLF, cursor to the last column, the redrawn character.
_SOFT_WRAP = re.compile(r"\r?\n\x1b\[\d+;\d+H.", re.DOTALL)
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_terminal(text: str) -> str:
    """Remove terminal escape sequences and carriage returns."""

    return _ESCAPES.sub("", _SOFT_WRAP.sub("", text or "")).replace("\r", "")


def unwrap(text: str) -> str:
    """Join hard-wrapped terminal output back into one line (no newlines kept)."""

    return strip_terminal(text).replace("\n", "")


def logical_lines(text: str, starts: tuple[str, ...]) -> list[str]:
    """Split output into lines, joining wrapped continuations onto their line.

    A physical line that does not begin with one of ``starts`` continues the
    previous logical line (it is what the terminal wrapped off its end).
    """

    lines: list[str] = []
    for raw in strip_terminal(text).split("\n"):
        if raw.startswith(starts) or not lines:
            lines.append(raw)
        else:
            lines[-1] += raw
    return [line.strip() for line in lines]
