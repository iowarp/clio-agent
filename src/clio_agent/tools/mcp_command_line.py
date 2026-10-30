"""Split a one-line MCP server command into argv, by the host platform's rules.

POSIX ``shlex`` reads a backslash as an escape, so on Windows it mangles
``C:\\Users\\me\\server.exe`` into ``C:Usersmeserver.exe`` (#1220): the spawn
then fails with an unrelated-looking "program not found". On Windows a command
line is split the way the Microsoft C runtime splits its own: a backslash is
literal unless it precedes a double quote, and quotes group words without
staying in the argument.
"""

from __future__ import annotations

import shlex
import sys

_WHITESPACE = " \t"


def split_command(text: str, *, windows: bool | None = None) -> list[str]:
    """``text`` as argv: Windows rules on Windows, POSIX ``shlex`` elsewhere.

    ``windows`` overrides the host check. An unclosed quote raises
    ``ValueError`` on either platform, as ``shlex.split`` does.
    """

    if windows is None:
        windows = sys.platform == "win32"
    return _split_windows(text) if windows else shlex.split(text)


def _split_windows(text: str) -> list[str]:
    """Microsoft C runtime argv rules.

    ``2n`` backslashes before a quote are ``n`` backslashes and the quote
    toggles quoting; ``2n + 1`` are ``n`` backslashes and a literal quote.
    Backslashes before anything else are literal.
    """

    args: list[str] = []
    current: list[str] = []
    in_arg = quoted = False
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            run = index
            while run < length and text[run] == "\\":
                run += 1
            count = run - index
            if run < length and text[run] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    run += 1
                index, in_arg = run, True
                continue
            current.append("\\" * count)
            index, in_arg = run, True
            continue
        if char == '"':
            quoted, in_arg = not quoted, True
        elif char in _WHITESPACE and not quoted:
            if in_arg:
                args.append("".join(current))
                current, in_arg = [], False
        else:
            current.append(char)
            in_arg = True
        index += 1
    if quoted:
        raise ValueError("No closing quotation")
    if in_arg:
        args.append("".join(current))
    return args
