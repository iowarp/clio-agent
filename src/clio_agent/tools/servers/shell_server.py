"""Local shell MCP server for CLIO utility commands.

The shell tool is intentionally small: it runs one command in a bounded
subprocess, returns stdout/stderr/exit code, and validates the working
directory against CLIO's file policy. The GACT permission gate treats tool
names containing ``shell`` as destructive, so agent-driven calls require the
normal user approval path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP

from clio_agent import conf
from clio_agent.runtime import trace
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError
from clio_agent.tools.servers.shell_output import (
    StreamCapture,
    compose_output_fields,
    read_process_stream,
    shell_result_char_budget,
)
from clio_agent.tools.servers.shell_spill_store import (
    SPILL_DIRNAME,
    active_session_id,
    new_call_id,
    spill_directory,
)

shell_server = FastMCP("shell")
logger = logging.getLogger(__name__)

# Declared MCP ToolAnnotations — the SINGLE source of truth for the shell tool's
# effect class (#1061). ``bash`` runs an arbitrary command whose writes/egress
# the gate cannot bound (they live behind the OS fence, not a gate-side parser),
# so it declares the most-restrictive open-world destructive hints: NOT read-only
# and openWorldHint=True. This projects to NO catalog read/write tag (effectful/
# unclassifiable) — preserving the pre-#1061 "no read tag => never read-only, and
# not an fs-write the auto-edits mode may auto-approve" classification.
_BASH_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "openWorldHint": True,
}

#: Namespaced tool name → declared annotations for the shell built-ins. Exported
#: so :mod:`clio_agent.tools.catalog` projects read/write tags from the SAME
#: mapping the decorator declares.
SHELL_TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {"shell_bash": _BASH_ANNOTATIONS}

# POSIX text utilities the model tends to reach for (and improvises `wsl bash -c`
# to get on Windows, booting a resident VM — iowarp/clio-agent#898). Their real
# presence on the host PATH is probed, not assumed.
_POSIX_TEXT_TOOLS = ("cut", "sed", "awk", "grep")

# Operational caps — resolved file → env → default (see clio_agent.conf).
# Timeouts: 0 means none. A command runs until it exits (a conversion, a batch
# job, a long `df`), the human cancels the turn, or the MODEL passes its own
# positive ``timeout_s``. An operator may still set a ceiling via
# ``limits.shell_max_timeout_s``; no ceiling is imposed by default.
_DEFAULT_TIMEOUT_S = conf.resolve(
    "limits.shell_default_timeout_s",
    env="CLIO_SHELL_DEFAULT_TIMEOUT_S",
    default=0.0,
    cast=conf.as_float,
)
_MAX_TIMEOUT_S = conf.resolve(
    "limits.shell_max_timeout_s", env="CLIO_SHELL_MAX_TIMEOUT_S", default=0.0, cast=conf.as_float
)


@dataclass(frozen=True)
class ShellLimits:
    """The effective shell limits, resolved from config (#1487).

    One object feeds BOTH the enforcement in :func:`bash` and the model-facing
    tool description, so the description can never drift from what is enforced.
    """

    max_command_chars: int
    default_output_bytes: int  # per stream, inline, when the caller passes none
    max_output_bytes: int  # per stream, the most a caller may request inline
    result_chars: int  # encoded-result budget (the tighter downstream bound)


def resolve_shell_limits() -> ShellLimits:
    """Resolve the shell limits from ``limits.shell_*`` (file -> env -> default).

    Defaults, and why:

    * ``shell_max_command_chars`` 16,000 (was 4,000, which rejected ordinary
      heredoc profiling scripts). On Windows the command rides the PowerShell /
      cmd command line, which CreateProcess caps at 32,767 characters; 16,000
      stays under that even if quoting doubled every character, and covers a
      ~200-line inline script. Past it, the rejection tells the model to write
      the script to a file and run the file.
    * ``shell_default_output_bytes`` 8,192 per stream (was 16,384). The result
      must fit the downstream 12,000-character tool-result bounds or it gets
      rewrapped into a truncated JSON-in-JSON preview (#887); 16 KiB made that
      the norm for default-sized output. Larger output spills to a file.
    * ``shell_max_output_bytes`` 131,072: the per-call ceiling. The encoded
      result is still held under ``result_chars``; the rest is in the spill file.
    """

    return ShellLimits(
        max_command_chars=conf.resolve(
            "limits.shell_max_command_chars",
            env="CLIO_SHELL_MAX_COMMAND_CHARS",
            default=16_000,
            cast=conf.as_int,
        ),
        default_output_bytes=conf.resolve(
            "limits.shell_default_output_bytes",
            env="CLIO_SHELL_DEFAULT_OUTPUT_BYTES",
            default=8 * 1024,
            cast=conf.as_int,
        ),
        max_output_bytes=conf.resolve(
            "limits.shell_max_output_bytes",
            env="CLIO_SHELL_MAX_OUTPUT_BYTES",
            default=128 * 1024,
            cast=conf.as_int,
        ),
        result_chars=shell_result_char_budget(),
    )


#: Resolved once at server build: the tool description below is registered from
#: it, and :func:`bash` enforces the same values.
_SHELL_LIMITS = resolve_shell_limits()
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
    """Resolve and validate a shell working directory.

    An explicit ``cwd`` argument always wins. Otherwise this pins to the active
    session's workspace root (bound per tool call by
    ``clio_agent.tools.execution.tool_workspace_context``) rather than the OS
    process's own current directory. A single managed backend process outlives
    its boot-time cwd and can serve sessions across multiple workspaces, so
    falling back to ``Path.cwd()`` let commands without an explicit ``cwd``
    run outside the session's own workspace (iowarp/clio-agent — desktop
    ``sensor_readings_*.md`` files landing in the install directory instead of
    the workspace). ``Path.cwd()`` remains the last-resort fallback for the
    app-less CLI grounding path where no workspace is bound.
    """

    if cwd:
        raw = Path(cwd).expanduser()
    else:
        from clio_agent.tools.execution import (  # noqa: PLC0415 - avoid import cycle
            get_active_tool_workspace_root,
        )

        active_root = get_active_tool_workspace_root()
        if active_root:
            raw = Path(active_root)
        else:
            # No silent fallback: a managed session with no bound workspace root
            # falling back to the OS process's own cwd (the install directory, on
            # desktop) is exactly the failure mode this function exists to avoid.
            # The app-less CLI grounding path legitimately has no workspace bound,
            # so this stays a typed trace event rather than a hard error.
            trace.event(
                "TOOLS",
                "shell cwd fallback reason=no_active_workspace_root cwd=%s",
                Path.cwd(),
            )
            raw = Path.cwd()
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
    # Advisory boundary: this validates only the working DIRECTORY, not the arbitrary paths
    # the command then writes (``> /etc/x``, ``python -c "open(...).write()"``). Those are
    # covered by the OS write-fence the enforcing twin composes in ``bash`` below
    # (:func:`clio_agent.runtime.sandbox.wrap_confined`, #976): on a fenced platform an
    # out-of-root write is DENIED (EROFS/EACCES) and minted as a ``policy_violation``; on the
    # floor it succeeds and is recorded as a ``gap``. This check remains the advisory twin.
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
        elif shutil.which("pwsh.exe"):
            shell_label = "PowerShell Core (pwsh.exe)"
        else:
            shell_label = "Windows PowerShell (powershell.exe)"
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


def _limits_text(limits: ShellLimits) -> str:
    """Model-facing statement of the effective limits and the spill behaviour."""

    return (
        f"Limits: the command may be up to {limits.max_command_chars} characters; for a "
        "longer script, write it to a file in the workspace first and run that file (for "
        "example `uv run python analysis.py`). stdout and stderr each return inline up to "
        f"max_output_bytes (default {limits.default_output_bytes}, at most "
        f"{limits.max_output_bytes}) and the whole result stays under "
        f"{limits.result_chars} characters. Output beyond that is not lost: the complete "
        f"stream is saved in the workspace's Agent-managed ``{SPILL_DIRNAME}/`` directory (one "
        "<id>.stdout.txt / .stderr.txt file per stream, kept until the session is "
        "deleted); `stdout` then holds a head excerpt cut on line boundaries and "
        "`stdout_spill` gives the file path, total_bytes, total_lines, and a tail excerpt. "
        "Read or grep that file instead of re-running the command."
    )


def build_shell_tool_description(facts: ShellEnvFacts, limits: ShellLimits | None = None) -> str:
    """Compose the shell ``bash`` tool description from host facts and limits.

    Pure function of its inputs so the per-platform content is unit-pinnable
    (#898): the Windows text carries an explicit 'do NOT assume WSL exists' and
    the POSIX text does not; both steer tabular/CSV work to the pandas MCP tool.
    The limits sentence is generated from ``limits`` (default: the effective
    :data:`_SHELL_LIMITS`), never hand-written numbers (#1487).
    """
    limits_text = _limits_text(limits or _SHELL_LIMITS)
    tools = ", ".join(_POSIX_TEXT_TOOLS)
    tools_line = (
        f"POSIX text tools ({tools}) ARE available on this host's PATH."
        if facts.posix_text_tools
        else f"POSIX text tools ({tools}) are NOT available on this host's PATH."
    )
    if facts.is_windows:
        is_bash = facts.shell_label.lower().startswith("bash")
        invocation = (
            "Windows drive paths in commands are translated to /mnt/<drive>/ for bash. "
            if is_bash
            else "The tool invokes PowerShell with -NoLogo -NoProfile -NonInteractive -Command. "
            if "PowerShell" in facts.shell_label
            else "The tool invokes cmd.exe with /d /s /c. "
        )
        return (
            f"Run ONE local shell command on a {facts.system_label} host and return "
            f"stdout, stderr, and exit code. Commands execute under {facts.shell_label} "
            f"semantics{' (this is NOT a bash/POSIX shell)' if not is_bash else ''}. "
            f"{invocation}"
            f"{tools_line} "
            "Do NOT assume WSL exists: do not invoke `wsl`, `wsl bash -c`, or POSIX "
            "pipelines to reach cut/sed/awk/grep — on a WSL-less host they fail, and the "
            "first `wsl` call boots a utility VM that stays resident holding gigabytes. "
            "Paths use Windows conventions (drive letters, backslashes) unless the bash "
            "backend is active. For tabular, "
            "CSV, or columnar work (column selection, filtering, joins) use the pandas "
            "MCP tool when available instead of shell text pipelines — it is portable and "
            "spawns no VM. The working directory must be inside CLIO_ALLOWED_ROOTS. "
            "A command can retrieve public HTTPS data when network policy permits; "
            "inspect its returned status and output before concluding access failed. "
            f"The command runs until it exits unless you pass timeout_s. {limits_text}"
        )
    return (
        f"Run ONE local shell command on a {facts.system_label} host and return stdout, "
        f"stderr, and exit code. Commands execute under {facts.shell_label}. "
        f"{tools_line} "
        "Paths use POSIX conventions (forward slashes). For large tabular, CSV, or "
        "columnar work (column selection, filtering, joins) prefer the pandas MCP tool "
        "when available over ad-hoc text pipelines. The working directory must be inside "
        "CLIO_ALLOWED_ROOTS. "
        "A command can retrieve public HTTPS data when network policy permits; "
        "inspect its returned status and output before concluding access failed. "
        f"The command runs until it exits unless you pass timeout_s. {limits_text}"
    )


#: Computed once at server build from the running host (#898). The model reads
#: this as the ``bash`` tool description, so it is grounded in the real platform,
#: shell, and tool availability rather than a static "bash"-flavoured string.
_SHELL_TOOL_DESCRIPTION = build_shell_tool_description(_detect_shell_env())


def _kill_process_tree(pid: int) -> None:
    """Kill ``pid`` and every descendant (``uv run`` and shells spawn children)."""

    import psutil  # noqa: PLC0415

    try:
        root = psutil.Process(pid)
        victims = [*root.children(recursive=True), root]
    except psutil.NoSuchProcess:
        return
    for proc in victims:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs(victims, timeout=5)


@shell_server.tool(description=_SHELL_TOOL_DESCRIPTION, annotations=_BASH_ANNOTATIONS)
async def bash(
    ctx: Context,
    command: str,
    cwd: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_output_bytes: int = _SHELL_LIMITS.default_output_bytes,
) -> dict[str, Any]:
    """Run one local shell command and return stdout, stderr, and exit code.

    The model-facing description is computed at server build from the host and
    the effective limits (:data:`_SHELL_TOOL_DESCRIPTION`, #898/#1487); this
    docstring is the developer reference. The command runs until it exits unless
    ``timeout_s`` is positive (or an operator ceiling applies) and the working
    directory must be inside ``CLIO_ALLOWED_ROOTS``. Output that does not fit the
    result budget is spilled in full under Agent-managed ``tool-output/`` state and excerpted
    (:mod:`clio_agent.tools.servers.shell_output`). A cancelled turn or a
    timeout kills the command's whole process tree.
    """

    if not isinstance(command, str) or not command.strip():
        return _error(
            "invalid_command",
            "command must be a non-empty string.",
            details={"field": "command"},
        )
    command = command.strip()
    max_chars = _SHELL_LIMITS.max_command_chars
    if len(command) > max_chars:
        next_action = (
            "Split the script into shorter shell calls that write a file inside the workspace, "
            "then run that file with a short command (for example `uv run python analysis.py`). "
            "fs_propose_edit only stages a reviewable diff; it does not write the file."
        )
        return _error(
            "command_too_long",
            f"command is {len(command)} characters; the limit is {max_chars}. {next_action}",
            details={
                "received_chars": len(command),
                "max_chars": max_chars,
                "next_action": next_action,
            },
        )
    try:
        timeout = float(timeout_s)
    except (TypeError, ValueError):
        return _error("invalid_timeout", "timeout_s must be a number.")
    if timeout < 0 or (_MAX_TIMEOUT_S > 0 and timeout > _MAX_TIMEOUT_S):
        ceiling = f" and <= {_MAX_TIMEOUT_S:g}" if _MAX_TIMEOUT_S > 0 else ""
        return _error(
            "invalid_timeout",
            f"timeout_s must be >= 0{ceiling} (0 means no timeout).",
            details={"received": timeout_s, "max_timeout_s": _MAX_TIMEOUT_S},
        )
    if timeout == 0 and _MAX_TIMEOUT_S > 0:
        timeout = _MAX_TIMEOUT_S
    if not isinstance(max_output_bytes, int) or isinstance(max_output_bytes, bool):
        return _error("invalid_max_output", "max_output_bytes must be an integer.")
    ceiling_bytes = _SHELL_LIMITS.max_output_bytes
    if max_output_bytes <= 0 or max_output_bytes > ceiling_bytes:
        return _error(
            "invalid_max_output",
            f"max_output_bytes must be > 0 and <= {ceiling_bytes}.",
            details={"received": max_output_bytes, "max_output_bytes": ceiling_bytes},
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
    run_env = {**os.environ, "PYTHONUTF8": "1", **confined.env_overlay}

    try:
        process = await asyncio.create_subprocess_exec(
            *run_argv,
            cwd=str(safe_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=run_env,
            **confined.popen_kwargs,
            # Give the child an immediately-EOF stdin. Without this the spawned
            # shell inherits clio-agent's own stdin (a pipe the parent holds open
            # and never closes), and PowerShell/cmd block at startup waiting on
            # that stream — every command then hits the timeout with empty output.
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        return _error("execution_failed", str(exc), details={"command": command})

    assert process.stdout is not None
    assert process.stderr is not None
    spill_root = _spill_root(safe_cwd)
    spill_dir = spill_directory(spill_root, session_id=active_session_id())
    call_id = new_call_id()
    captures = [
        StreamCapture(
            name,
            inline_limit=max_output_bytes,
            spill_path=spill_dir / f"{call_id}.{name}.txt",
        )
        for name in ("stdout", "stderr")
    ]
    readers = [
        asyncio.create_task(read_process_stream(pipe, capture=capture, ctx=ctx))
        for pipe, capture in zip((process.stdout, process.stderr), captures, strict=True)
    ]
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout or None)
    except TimeoutError:
        timed_out = True
        await asyncio.to_thread(_kill_process_tree, process.pid)
        await process.wait()
    except asyncio.CancelledError:
        # The turn was cancelled (the human stopped it): never orphan the command.
        _kill_process_tree(process.pid)
        raise
    await asyncio.gather(*readers)
    base = {
        "command": command,
        "cwd": str(safe_cwd),
        "exit_code": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "timeout_s": timeout,
    }
    return compose_output_fields(base, captures, root=spill_root, budget=shell_result_char_budget())


def _spill_root(safe_cwd: Path) -> Path:
    """Where this call's spill files go: the session workspace root.

    The model reads spilled output with its workspace file tools, so the files
    belong under the bound workspace's ``.clio`` root even when the command ran
    in an explicit sub-``cwd``. With no workspace bound (the app-less CLI path,
    whose fallback :func:`_resolve_cwd` already traces) the policy-validated cwd
    is used.
    """

    from clio_agent.tools.execution import (  # noqa: PLC0415 - avoid import cycle
        get_active_tool_workspace_root,
    )

    active_root = get_active_tool_workspace_root()
    return Path(active_root).resolve() if active_root else safe_cwd


__all__ = [
    "SHELL_TOOL_ANNOTATIONS",
    "ShellEnvFacts",
    "ShellLimits",
    "build_shell_tool_description",
    "resolve_shell_limits",
    "shell_server",
]
