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

__all__ = ["SHELL_TOOL_NAMES", "command_is_read_only", "shell_call_is_read_only", "shell_dialect"]

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
