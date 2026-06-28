"""Local shell MCP server for CLIO utility commands.

The shell tool is intentionally small: it runs one command in a bounded
subprocess, returns stdout/stderr/exit code, and validates the working
directory against CLIO's file policy. The GACT permission gate treats tool
names containing ``shell`` as destructive, so agent-driven calls require the
normal user approval path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from clio_agent import conf
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError

shell_server = FastMCP("shell")

# Operational caps — resolved file → env → default (see clio_agent.conf).
_DEFAULT_TIMEOUT_S = conf.resolve(
    "limits.shell_default_timeout_s",
    env="CLIO_SHELL_DEFAULT_TIMEOUT_S",
    default=5.0,
    cast=conf.as_float,
)
_MAX_TIMEOUT_S = conf.resolve(
    "limits.shell_max_timeout_s", env="CLIO_SHELL_MAX_TIMEOUT_S", default=30.0, cast=conf.as_float
)
_DEFAULT_MAX_OUTPUT_BYTES = conf.resolve(
    "limits.shell_default_output_bytes",
    env="CLIO_SHELL_DEFAULT_OUTPUT_BYTES",
    default=16 * 1024,
    cast=conf.as_int,
)
_MAX_OUTPUT_BYTES = conf.resolve(
    "limits.shell_max_output_bytes",
    env="CLIO_SHELL_MAX_OUTPUT_BYTES",
    default=128 * 1024,
    cast=conf.as_int,
)
_MAX_COMMAND_CHARS = conf.resolve(
    "limits.shell_max_command_chars",
    env="CLIO_SHELL_MAX_COMMAND_CHARS",
    default=4000,
    cast=conf.as_int,
)
_WINDOWS_BASH_PATH = re.compile(
    r"(?P<drive>[A-Za-z]):[\\/](?P<rest>[^\"'`\s|&;<>()]+(?:[\\/][^\"'`\s|&;<>()]+)*)"
)


def _error(code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the structured error shape used by CLIO tools."""

    return {
        "error": {
            "type": "shell",
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


def _resolve_cwd(cwd: str | None) -> Path:
    """Resolve and validate a shell working directory."""

    raw = Path(cwd).expanduser() if cwd else Path.cwd()
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FilePolicyError(
            code="cwd_not_found",
            message=f"Working directory does not exist: {raw}",
            field="cwd",
            path=str(raw),
            next_action="Choose an existing directory inside CLIO_ALLOWED_ROOTS.",
        ) from exc
    if not resolved.is_dir():
        raise FilePolicyError(
            code="cwd_not_directory",
            message=f"Working directory is not a directory: {resolved}",
            field="cwd",
            path=str(resolved),
            next_action="Choose a directory inside CLIO_ALLOWED_ROOTS.",
        )
    FileAccessPolicy.from_env()._ensure_allowed(resolved, field="cwd")
    return resolved


def _windows_bash_path(path_match: re.Match[str]) -> str:
    """Translate a Windows drive path to the WSL mount path bash expects."""

    drive = path_match.group("drive").lower()
    rest = path_match.group("rest").replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _translate_windows_paths_for_bash(command: str) -> str:
    """Translate embedded Windows absolute paths before sending to WSL bash."""

    return _WINDOWS_BASH_PATH.sub(_windows_bash_path, command)


def _windows_shell_backend() -> str:
    """Return the configured Windows shell backend."""

    backend = conf.resolve(
        "tools.shell.windows_backend",
        env="CLIO_WINDOWS_SHELL_BACKEND",
        default="powershell",
        cast=conf.as_str,
    )
    normalized = backend.strip().lower()
    return normalized if normalized in {"powershell", "bash", "cmd"} else "powershell"


def _shell_argv(command: str) -> list[str]:
    """Return a platform-appropriate shell invocation."""

    if os.name == "nt":
        backend = _windows_shell_backend()
        if backend == "bash":
            bash = shutil.which("bash.exe") or shutil.which("bash")
            if bash:
                return [bash, "-lc", _translate_windows_paths_for_bash(command)]
        powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if powershell and backend != "cmd":
            return [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ]
        return ["cmd.exe", "/d", "/s", "/c", command]
    shell = shutil.which("bash") or shutil.which("sh")
    if shell is None:
        raise RuntimeError("no POSIX shell found on PATH")
    return [shell, "-lc", command]


def _clip_output(text: str, max_bytes: int) -> tuple[str, bool]:
    """Clip text by UTF-8 byte size while preserving valid text."""

    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    clipped = raw[:max_bytes].decode("utf-8", errors="replace")
    return clipped, True


@shell_server.tool()
def bash(
    command: str,
    cwd: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Run one local shell command and return stdout, stderr, and exit code.

    Use for simple local utility checks such as the current time, environment
    diagnostics, or file listings. The command runs in a subprocess with a
    strict timeout and output cap. The working directory must be inside
    ``CLIO_ALLOWED_ROOTS``.
    """

    if not isinstance(command, str) or not command.strip():
        return _error(
            "invalid_command",
            "command must be a non-empty string.",
            details={"field": "command"},
        )
    command = command.strip()
    if len(command) > _MAX_COMMAND_CHARS:
        return _error(
            "command_too_long",
            f"command must be <= {_MAX_COMMAND_CHARS} characters.",
            details={"received_chars": len(command), "max_chars": _MAX_COMMAND_CHARS},
        )
    try:
        timeout = float(timeout_s)
    except (TypeError, ValueError):
        return _error("invalid_timeout", "timeout_s must be a number.")
    if timeout <= 0 or timeout > _MAX_TIMEOUT_S:
        return _error(
            "invalid_timeout",
            f"timeout_s must be > 0 and <= {_MAX_TIMEOUT_S:g}.",
            details={"received": timeout_s, "max_timeout_s": _MAX_TIMEOUT_S},
        )
    if not isinstance(max_output_bytes, int) or isinstance(max_output_bytes, bool):
        return _error("invalid_max_output", "max_output_bytes must be an integer.")
    if max_output_bytes <= 0 or max_output_bytes > _MAX_OUTPUT_BYTES:
        return _error(
            "invalid_max_output",
            f"max_output_bytes must be > 0 and <= {_MAX_OUTPUT_BYTES}.",
            details={"received": max_output_bytes, "max_output_bytes": _MAX_OUTPUT_BYTES},
        )
    try:
        safe_cwd = _resolve_cwd(cwd)
        argv = _shell_argv(command)
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:  # noqa: BLE001
        return _error("shell_unavailable", str(exc))

    try:
        completed = subprocess.run(
            argv,
            cwd=str(safe_cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stdout, stdout_truncated = _clip_output(stdout, max_output_bytes)
        stderr, stderr_truncated = _clip_output(stderr, max_output_bytes)
        return {
            "command": command,
            "cwd": str(safe_cwd),
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "timeout_s": timeout,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
    except Exception as exc:  # noqa: BLE001
        return _error("execution_failed", str(exc), details={"command": command})

    stdout, stdout_truncated = _clip_output(completed.stdout, max_output_bytes)
    stderr, stderr_truncated = _clip_output(completed.stderr, max_output_bytes)
    return {
        "command": command,
        "cwd": str(safe_cwd),
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "timeout_s": timeout,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


__all__ = ["shell_server"]
