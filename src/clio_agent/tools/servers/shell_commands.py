"""Command-launch bounds and shared recovery instructions for shell callers."""

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
