"""Command-launch preambles, bounds and shared recovery instructions for shell callers."""

from __future__ import annotations

import errno
import re
import subprocess
from pathlib import PureWindowsPath
from typing import Any

_WINDOWS_BASH_PATH = re.compile(
    r"(?P<drive>[A-Za-z]):[\\/](?P<rest>[^\"'`\s|&;<>()]+(?:[\\/][^\"'`\s|&;<>()]+)*)"
)


def _windows_bash_path(path_match: re.Match[str]) -> str:
    """Translate a Windows drive path to the WSL mount path bash expects."""
    drive = path_match.group("drive").lower()
    rest = path_match.group("rest").replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def translate_windows_paths_for_bash(command: str) -> str:
    """Translate embedded Windows absolute paths before sending to WSL bash."""
    return _WINDOWS_BASH_PATH.sub(_windows_bash_path, command)


SCRIPT_FILE_GUIDANCE = (
    "Write the program to a workspace .py, .mjs or .ps1 file with an available file-writing "
    "tool, or several small shell calls (about 1-2 KB each on Windows), then run that file "
    "with a short command using the prepared runtime arguments. Putting the whole program "
    "in one here-string, heredoc, -c argument or encoded command still hits launcher limits."
)


def command_length_error(
    command: str,
    message: str,
    *,
    bound: str,
    max_chars: int | None = None,
    launch_chars: int | None = None,
) -> dict[str, Any]:
    """Return a recoverable length error without repeating the oversized program."""
    details: dict[str, Any] = {
        "received_chars": len(command),
        "bound": bound,
        "next_action": SCRIPT_FILE_GUIDANCE,
    }
    if max_chars is not None:
        details["max_chars"] = max_chars
    if launch_chars is not None:
        details["launch_chars"] = launch_chars
    return {
        "error": {
            "type": "shell",
            "code": "command_too_long",
            "message": f"{message} {SCRIPT_FILE_GUIDANCE}",
            "details": details,
        }
    }


def windows_launch_error(argv: list[str], command: str) -> dict[str, Any] | None:
    """Check the quoted UTF-16 launch string against its actual Windows entry point."""
    launcher = PureWindowsPath(argv[0])
    batch = launcher.name.lower() in {"cmd", "cmd.exe"} or launcher.suffix.lower() in {
        ".cmd",
        ".bat",
    }
    # cmd.exe caps the command at 8191 characters. CreateProcessW's 32767
    # UTF-16-code-unit bound includes the terminating NUL.
    maximum = 8191 if batch else 32766
    length = len(subprocess.list2cmdline(argv).encode("utf-16-le", errors="surrogatepass")) // 2
    if length <= maximum:
        return None
    return command_length_error(
        command,
        f"Windows launcher {launcher.name} needs {length} UTF-16 characters; its limit is {maximum}.",
        bound="windows_launcher",
        max_chars=maximum,
        launch_chars=length,
    )


def is_command_length_exception(error: Exception) -> bool:
    """Recognize native spawn length errors independently of localized error text."""
    return isinstance(error, OSError) and (
        error.errno == errno.E2BIG or getattr(error, "winerror", None) == 206
    )


def is_command_length_output(text: str) -> bool:
    """Recognize the cmd.exe diagnostic when its launcher starts but rejects its input."""
    return any(
        line.strip().removesuffix(".").lower() == "the command line is too long"
        for line in text.splitlines()
    )


#: Prefixed to every PowerShell command so text crosses the process boundary as UTF-8,
#: the encoding the shell tool decodes. Without a console (the server's case) PowerShell
#: writes and pipes in the OEM code page, so an em dash comes back as ``-`` or ``?``, and
#: Windows PowerShell 5.1 reads a BOM-less UTF-8 file as ANSI (``—`` → ``â€”``). Only the
#: readers' default changes: an explicit ``-Encoding`` still wins, a BOM is still honoured,
#: and writing cmdlets keep their defaults. Setting the console encoding fails harmlessly
#: when there is no console handle to set.
POWERSHELL_UTF8_PREAMBLE = (
    "try{[Console]::OutputEncoding=[Console]::InputEncoding="
    "[System.Text.UTF8Encoding]::new($false)}catch{};"
    "$OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
    "foreach($c in 'Get-Content','Select-String','Import-Csv')"
    "{$PSDefaultParameterValues[\"${c}:Encoding\"]='UTF8'};"
)

#: Windows PowerShell 5.1 runs on .NET Framework, whose web stack (``Invoke-WebRequest``,
#: ``Invoke-RestMethod``, ``WebClient``) ignores ``HTTPS_PROXY`` and reads only the per-user
#: WinINet settings, so inside the fence it dialled out directly and was refused. This hands
#: the process the same ``http://`` proxy every other client in the shell already uses (the
#: fence's chokepoint, or the person's own), local names bypassed. PowerShell 7 honours the
#: variables itself and is left alone; with no proxy variable nothing changes.
POWERSHELL_PROXY_PREAMBLE = (
    "if($PSVersionTable.PSEdition -eq 'Desktop'){"
    "$p=@($env:HTTPS_PROXY,$env:ALL_PROXY,$env:HTTP_PROXY)|?{$_ -like 'http://*'}|select -First 1;"
    "if($p){try{[System.Net.WebRequest]::DefaultWebProxy="
    "New-Object System.Net.WebProxy($p,$true)}catch{}}};"
)

#: Windows PowerShell 5.1 resolves a command the way current PowerShell does. PowerShell 6
#: dropped exactly the default aliases that shadowed native Windows programs: ``curl`` and
#: ``wget`` (``Invoke-WebRequest``; PowerShell/PowerShell#1901, never shipped in PowerShell
#: Core) and ``sc`` (``Set-Content`` over ``sc.exe``, #5827). They sit in the
#: ``#if !CORECLR`` block of ``InitialSessionState.cs`` ("not appropriate for PSCore6 due to
#: conflicts"). In 5.1, ``curl -sS URL`` therefore failed on a parameter binding although
#: ``curl.exe`` is installed. Every other alias (``ls``, ``rm``, ``cat``, ``where``, ...) is
#: kept, as in current PowerShell, so ``ls | Where-Object ...`` and ``rm -Recurse`` keep
#: meaning the cmdlets. The aliases are removed in the command's own scope, because removing
#: an ``AllScope`` alias from a child scope only drops that scope's copy, and through the
#: provider API rather than ``Remove-Item``, whose first call in a fresh process costs ~50 ms.
POWERSHELL_ALIAS_PREAMBLE = (
    "if($PSVersionTable.PSEdition -eq 'Desktop'){foreach($a in 'curl','wget','sc'){"
    "try{$ExecutionContext.SessionState.InvokeProvider.Item.Remove("
    '[string[]]"alias:\\$a",$false,$true,$true)}catch{}};'
    "$ExecutionContext.SessionState.PSVariable.Remove('a')};"
)

#: Everything CLIO prefixes to a PowerShell command, in order.
POWERSHELL_PREAMBLE = (
    POWERSHELL_UTF8_PREAMBLE + POWERSHELL_PROXY_PREAMBLE + POWERSHELL_ALIAS_PREAMBLE
)
