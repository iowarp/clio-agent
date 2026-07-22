"""Local shell MCP server for CLIO utility commands.

The shell tool is intentionally small: it runs one command in a bounded
subprocess, returns stdout/stderr/exit code, and validates the working
directory against CLIO's file policy. The GACT permission gate treats tool
names containing ``shell`` as destructive, so agent-driven calls require the
normal user approval path.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from clio_agent import conf
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError

shell_server = FastMCP("shell")

# POSIX text utilities the model tends to reach for (and improvises `wsl bash -c`
# to get on Windows, booting a resident VM — iowarp/clio-agent#898). Their real
# presence on the host PATH is probed, not assumed.
_POSIX_TEXT_TOOLS = ("cut", "sed", "awk", "grep")

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


@dataclass(frozen=True)
class ShellEnvFacts:
    """Host facts the shell tool description is computed from (#898).

    The tool description the model reads must describe the environment the
    command ACTUALLY executes in — the model improvised ``wsl bash -c "cut ..."``
    on Windows precisely because the tool called itself a *bash* and the host was
    not one. This is grounding, not a behavioural handcuff (CLAUDE.md ⚑ #3).
    """

    is_windows: bool
    system_label: str  # e.g. "Windows", "Linux", "Darwin"
    shell_label: str  # effective shell + version, e.g. "PowerShell 5.1", "bash 5.2"
    posix_text_tools: bool  # cut/sed/awk/grep present on PATH


def _detect_shell_env() -> ShellEnvFacts:
    """Detect the effective shell environment for the running host (#898).

    Reads the platform, the effective shell backend (honouring
    :func:`_windows_shell_backend`, matching what :func:`_shell_argv` actually
    invokes), and whether the POSIX text tools are truly present on PATH. Platform
    detection is via ``os.name`` so tests can monkeypatch it.

    Deliberately spawns **no subprocess**: this runs at server build (module
    import), a hot path for every gact/CLI/test boot, so it must not block on a
    shell version probe. The shell *name* (not a live version string) is enough
    grounding to stop the model calling a POSIX shell a "bash".
    """
    is_windows = os.name == "nt"
    posix_text_tools = all(shutil.which(tool) for tool in _POSIX_TEXT_TOOLS)

    if is_windows:
        backend = _windows_shell_backend()
        if backend == "bash" and (shutil.which("bash.exe") or shutil.which("bash")):
            shell_label = "bash (Windows, e.g. Git Bash)"
        elif backend == "cmd" or not (shutil.which("pwsh.exe") or shutil.which("powershell.exe")):
            shell_label = "cmd.exe" if backend == "cmd" else "cmd.exe (PowerShell not on PATH)"
        else:
            shell_label = "PowerShell"
        return ShellEnvFacts(
            is_windows=True,
            system_label=platform.system() or "Windows",
            shell_label=shell_label,
            posix_text_tools=posix_text_tools,
        )

    shell = shutil.which("bash") or shutil.which("sh") or "sh"
    return ShellEnvFacts(
        is_windows=False,
        system_label=platform.system() or "POSIX",
        shell_label=Path(shell).name,
        posix_text_tools=posix_text_tools,
    )


def build_shell_tool_description(facts: ShellEnvFacts) -> str:
    """Compose the shell ``bash`` tool description from host facts (#898).

    Pure function of ``facts`` so the per-platform content is unit-pinnable: the
    Windows text carries an explicit 'do NOT assume WSL exists' and the POSIX text
    does not. Both steer tabular/CSV work to the pandas MCP tool.
    """
    tools = ", ".join(_POSIX_TEXT_TOOLS)
    tools_line = (
        f"POSIX text tools ({tools}) ARE available on this host's PATH."
        if facts.posix_text_tools
        else f"POSIX text tools ({tools}) are NOT available on this host's PATH."
    )
    if facts.is_windows:
        return (
            f"Run ONE local shell command on a {facts.system_label} host and return "
            f"stdout, stderr, and exit code. Commands execute under {facts.shell_label} "
            "semantics (this is NOT a bash/POSIX shell). "
            f"{tools_line} "
            "Do NOT assume WSL exists: do not invoke `wsl`, `wsl bash -c`, or POSIX "
            "pipelines to reach cut/sed/awk/grep — on a WSL-less host they fail, and the "
            "first `wsl` call boots a utility VM that stays resident holding gigabytes. "
            "Paths use Windows conventions (drive letters, backslashes). For tabular, "
            "CSV, or columnar work (column selection, filtering, joins) use the pandas "
            "MCP tool when available instead of shell text pipelines — it is portable and "
            "spawns no VM. The working directory must be inside CLIO_ALLOWED_ROOTS; the "
            "command runs in a subprocess with a strict timeout and output cap."
        )
    return (
        f"Run ONE local shell command on a {facts.system_label} host and return stdout, "
        f"stderr, and exit code. Commands execute under {facts.shell_label}. "
        f"{tools_line} "
        "Paths use POSIX conventions (forward slashes). For large tabular, CSV, or "
        "columnar work (column selection, filtering, joins) prefer the pandas MCP tool "
        "when available over ad-hoc text pipelines. The working directory must be inside "
        "CLIO_ALLOWED_ROOTS; the command runs in a subprocess with a strict timeout and "
        "output cap."
    )


#: Computed once at server build from the running host (#898). The model reads
#: this as the ``bash`` tool description, so it is grounded in the real platform,
#: shell, and tool availability rather than a static "bash"-flavoured string.
_SHELL_TOOL_DESCRIPTION = build_shell_tool_description(_detect_shell_env())


def _clip_output(text: str, max_bytes: int) -> tuple[str, bool]:
    """Clip text by UTF-8 byte size while preserving valid text."""

    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text, False
    clipped = raw[:max_bytes].decode("utf-8", errors="replace")
    return clipped, True


@shell_server.tool(description=_SHELL_TOOL_DESCRIPTION)
def bash(
    command: str,
    cwd: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Run one local shell command and return stdout, stderr, and exit code.

    The model-facing description is computed at server build from the host
    (:data:`_SHELL_TOOL_DESCRIPTION`) so it names the real platform, effective
    shell, and POSIX-tool availability (#898); this docstring is the developer
    reference. The command runs in a subprocess with a strict timeout and output
    cap; the working directory must be inside ``CLIO_ALLOWED_ROOTS``.
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

    # #975: route the shell subprocess through the single confinement composer with the
    # per-invocation `shell` profile (write territory computed from THIS command's cwd).
    # Floor-first — the backend is passthrough this slice, so argv/env are byte-identical;
    # the shell seam carries no pdeathsig today, so it stays off here.
    from clio_agent.runtime import sandbox  # noqa: PLC0415 - avoid import cycle

    confined = sandbox.wrap_confined(
        argv[0],
        argv[1:],
        write_roots=sandbox.effective_write_roots(
            sandbox.PROFILE_SHELL, workspace_root=str(safe_cwd)
        ),
        net_policy=sandbox.NET_ALLOW_RECORD,
        profile=sandbox.PROFILE_SHELL,
        pdeathsig=False,
    )
    run_argv = [confined.command, *confined.args]
    run_env = {**os.environ, **confined.env_overlay} if confined.env_overlay else None

    try:
        completed = subprocess.run(
            run_argv,
            cwd=str(safe_cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=run_env,
            **confined.popen_kwargs,
            # Give the child an immediately-EOF stdin. Without this the spawned
            # shell inherits clio-agent's own stdin (a pipe the parent holds open
            # and never closes), and PowerShell/cmd block at startup waiting on
            # that stream — every command then hits the timeout with empty output.
            stdin=subprocess.DEVNULL,
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


__all__ = [
    "ShellEnvFacts",
    "build_shell_tool_description",
    "shell_server",
]
