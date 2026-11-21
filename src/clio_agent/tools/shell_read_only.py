"""Recognise a shell command that only lists or reads, so the gate can treat it as a read.

The shell tool declares itself destructive (``builtin_annotations._BASH_ANNOTATIONS``):
an arbitrary command can do anything. A command made only of plain listing and reading
commands (``Get-ChildItem``, ``Get-Content``, ``Test-Path``, ``Select-String``, ``ls``,
``cat`` and their aliases), optionally piped through formatting and filtering commands,
changes nothing. :func:`shell_call_is_read_only` recognises that case and nothing else.

It is an allowlist over a quote-aware scan, and it is deliberately conservative. A
command is a read only when every segment between ``|``, ``;``, ``&&``, ``||`` and new
lines starts with an allowlisted command, and the command has no redirection (other
than discarding or merging stderr), no subexpression, script block, grouping, call or
escape character, and no command substitution. Anything it cannot prove is not a read,
and the gate decides that call as before. The OS fence still contains every command.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from typing import Any

__all__ = [
    "SHELL_TOOL_NAMES",
    "command_is_destructive",
    "command_is_read_only",
    "shell_call_is_read_only",
    "shell_call_subject",
    "shell_dialect",
]

#: The gate sees the shell tool under its catalog name or the native task name.
SHELL_TOOL_NAMES = frozenset({"shell_bash", "bash"})

_MAX_COMMAND_CHARS = 4000

#: PowerShell (case-insensitive): reading cmdlets and the pipeline commands that
#: only shape, filter or discard their input. Their parameters cannot write.
_POWERSHELL_READS = frozenset(
    {
        "get-childitem",
        "gci",
        "dir",
        "ls",
        "get-content",
        "gc",
        "cat",
        "type",
        "test-path",
        "select-string",
        "sls",
        "get-item",
        "gi",
        "get-itemproperty",
        "gp",
        "get-location",
        "gl",
        "pwd",
        "resolve-path",
        "rvpa",
        "split-path",
        "join-path",
        "get-filehash",
        "measure-object",
        "measure",
        "select-object",
        "select",
        "sort-object",
        "sort",
        "where-object",
        "where",
        "?",
        "group-object",
        "group",
        "format-table",
        "ft",
        "format-list",
        "fl",
        "format-wide",
        "fw",
        "out-string",
        "out-null",
        "write-output",
        "echo",
        "write",
        "write-host",
        "convertto-json",
        "convertfrom-json",
        "get-command",
        "gcm",
        "get-date",
    }
)

#: POSIX shells (case-sensitive). Commands with a writing option (``sort -o``,
#: ``uniq IN OUT``, ``tree -o``, ``file -C``) or that run other commands
#: (``find -exec``, ``xargs``, ``env``) are left out.
_POSIX_READS = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "grep",
        "egrep",
        "fgrep",
        "pwd",
        "stat",
        "du",
        "df",
        "echo",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "which",
        "md5sum",
        "sha1sum",
        "sha256sum",
    }
)

#: cmd.exe (case-insensitive).
_CMD_READS = frozenset({"dir", "type", "findstr", "where", "echo"})

_READS = {"powershell": _POWERSHELL_READS, "posix": _POSIX_READS, "cmd": _CMD_READS}

#: Discarding or merging stderr writes nothing: ``2>$null``, ``2>&1``, ``2>/dev/null``,
#: ``2>nul`` and PowerShell's ``*>$null``.
_HARMLESS_REDIRECT = re.compile(
    r"(?<![\w>&])(?:2|\*)>\s*(?:\$null|&1|/dev/null|nul)(?=$|[\s|;&])", re.I
)

#: Outside quotes, these characters mean redirection, grouping, a script block, a
#: subexpression, an escape or command substitution: never a plain read.
_FORBIDDEN = {
    "powershell": frozenset("<>(){}`&"),
    "posix": frozenset("<>(){}`\\"),
    "cmd": frozenset("<>()^"),
}


def shell_dialect() -> str:
    """The dialect the shell tool runs commands in on this host (``_shell_argv``)."""

    if os.name != "nt":
        return "posix"
    from clio_agent.tools.servers.shell_server import _windows_shell_backend  # noqa: PLC0415

    backend = _windows_shell_backend()
    if backend == "bash" and (shutil.which("bash.exe") or shutil.which("bash")):
        return "posix"
    if backend != "cmd" and (shutil.which("pwsh.exe") or shutil.which("powershell.exe")):
        return "powershell"
    return "cmd"


def _segments(command: str, dialect: str) -> list[str] | None:
    """Split ``command`` into pipeline and list segments, or ``None`` if it is not plain."""

    forbidden = _FORBIDDEN[dialect]
    single_quotes = dialect != "cmd"  # cmd.exe has no single-quoted strings
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and dialect != "cmd" and (ch == "`" or command[i : i + 2] == "$("):
                return None  # command substitution or a subexpression inside a string
            current.append(ch)
            i += 1
            continue
        if ch == '"' or (ch == "'" and single_quotes):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch in "|;\n\r" or (ch == "&" and dialect != "powershell"):
            if command[i : i + 2] in ("||", "&&"):
                i += 1
            segments.append("".join(current))
            current = []
            i += 1
            continue
        if ch == "&" and command[i : i + 2] == "&&":  # PowerShell 7's && chains
            segments.append("".join(current))
            current = []
            i += 2
            continue
        if ch in forbidden or command[i : i + 2] == "$(":
            return None
        current.append(ch)
        i += 1
    if quote:
        return None
    segments.append("".join(current))
    return segments


def command_is_read_only(command: str, dialect: str) -> bool:
    """Whether ``command`` only lists or reads, in shell ``dialect``.

    Args:
        command: The command line the shell tool would run.
        dialect: ``powershell``, ``posix`` or ``cmd`` (:func:`shell_dialect`).

    Returns:
        ``True`` only when every segment starts with an allowlisted read command and
        nothing in the line can redirect, execute or write.
    """

    reads = _READS.get(dialect)
    if reads is None or not isinstance(command, str):
        return False
    if not command.strip() or len(command) > _MAX_COMMAND_CHARS:
        return False
    segments = _segments(_HARMLESS_REDIRECT.sub(" ", command), dialect)
    if not segments:
        return False
    words = [segment.split(None, 1) for segment in segments]
    if any(not w for w in words):
        return False  # an empty segment: a dangling or doubled separator
    names = [w[0] if dialect == "posix" else w[0].lower() for w in words]
    return all(name in reads for name in names)


def shell_call_is_read_only(args: Mapping[str, Any] | None) -> bool:
    """Whether a shell tool call only lists or reads, inside the fence.

    A call that asks to run outside the sandbox is never treated as a read.
    """

    if not isinstance(args, Mapping) or args.get("outside_sandbox"):
        return False
    return command_is_read_only(args.get("command"), shell_dialect())


#: Commands that delete, overwrite, move or kill when they start a segment. Matched
#: case-insensitively in every dialect, so a POSIX ``rm`` on Windows still counts.
_DESTRUCTIVE_COMMANDS = frozenset(
    {
        # POSIX
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "truncate",
        "dd",
        "mv",
        "kill",
        "pkill",
        "killall",
        # PowerShell aliases and cmd.exe
        "ri",
        "del",
        "erase",
        "rd",
        "mi",
        "move",
        "clc",
        "sc",
        "spps",
        "format",
    }
)

#: PowerShell cmdlets that delete, overwrite, move or kill. Their names are distinctive,
#: so they count anywhere in the line (a nested ``pwsh -Command "Remove-Item ..."`` too).
_DESTRUCTIVE_CMDLETS = re.compile(
    r"(?<![\w-])(?:remove-(?:item|itemproperty)|clear-(?:content|item|disk|recyclebin)"
    r"|set-content|out-file|move-item|stop-process|format-volume|initialize-disk)(?![\w-])",
    re.I,
)

#: Arguments or .NET calls that delete or overwrite whatever command carries them.
_DESTRUCTIVE_PATTERNS = re.compile(
    r"(?<![\w-])(?:-delete|--delete|--force|-rf|-fr)(?![\w-])"
    r"|(?<![\w-])(?:-X|--method|-Method)\s*['\"]?delete\b"
    r"|(?<![\w-])sed\s+(?:-\w*i|--in-place)"
    r"|(?<![\w-])git\s+(?:reset\s+--hard|clean|checkout\s+--|restore|push\s+(?:-f|--force))"
    r"|::\s*(?:delete|move|replace|writeall\w*)\s*\(",
    re.I,
)

_REDIRECT = re.compile(r">")


def _command_words(command: str) -> tuple[list[str], str]:
    """The first word of each segment, and the line with quoted text removed."""

    words: list[str] = []
    unquoted: list[str] = []
    current: list[str] = []
    quote = ""
    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
            current.append(" ")
            continue
        if ch in "\"'":
            quote = ch
            current.append(" ")
            continue
        unquoted.append(ch)
        if ch in "|;&\n\r({":
            words.extend(("".join(current).split() or [""])[:1])
            current = []
            unquoted.append(" ")
            continue
        current.append(ch)
    words.extend(("".join(current).split() or [""])[:1])
    return [w.lower() for w in words if w], "".join(unquoted)


def command_is_destructive(command: str) -> bool:
    """Whether ``command`` visibly deletes, overwrites, moves or kills something.

    This names a call for the person approving it; it never decides whether the call
    asks. Anything this cannot read (an over-long line, a non-string) counts as
    destructive. A command that only runs a program or script is not destructive by
    this test, although the program may still write inside the fence.
    """

    if not isinstance(command, str) or len(command) > _MAX_COMMAND_CHARS:
        return True
    if _DESTRUCTIVE_CMDLETS.search(command) or _DESTRUCTIVE_PATTERNS.search(command):
        return True
    words, unquoted = _command_words(_HARMLESS_REDIRECT.sub(" ", command))
    if any(word.rsplit("/", 1)[-1] in _DESTRUCTIVE_COMMANDS for word in words):
        return True
    return bool(_REDIRECT.search(unquoted))


def shell_call_subject(args: Mapping[str, Any] | None) -> str:
    """The gate's noun for a shell call that is not a read.

    ``shell command`` for one that runs a program or script, ``destructive tool`` for
    one that visibly deletes, overwrites, moves or kills, or that runs outside the
    sandbox, or whose command cannot be read.
    """

    if not isinstance(args, Mapping) or args.get("outside_sandbox"):
        return "destructive tool"
    command = args.get("command")
    if not isinstance(command, str) or not command.strip() or command_is_destructive(command):
        return "destructive tool"
    return "shell command"
