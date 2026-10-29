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
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Protocol

from fastmcp import Context, FastMCP
from pydantic import Field

from clio_agent import conf
from clio_agent.runtime import trace
from clio_agent.tools.builtin_annotations import (
    _BASH_ANNOTATIONS,
)
from clio_agent.tools.builtin_annotations import (
    SHELL_TOOL_ANNOTATIONS as SHELL_TOOL_ANNOTATIONS,
)
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError
from clio_agent.tools.servers.shell_commands import (
    SCRIPT_FILE_GUIDANCE,
    command_length_error,
    is_command_length_exception,
    windows_launch_error,
)
from clio_agent.tools.servers.shell_commands import (
    translate_windows_paths_for_bash as _translate_windows_paths_for_bash,
)
from clio_agent.tools.servers.shell_output import (
    ProgressReporter,
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
from clio_agent.tools.servers.shell_spill_store import spill_root as _spill_root

shell_server = FastMCP("shell")
logger = logging.getLogger(__name__)

# Declared MCP ToolAnnotations â€” the SINGLE source of truth for the shell tool's
# effect class (#1061). ``bash`` runs an arbitrary command whose writes/egress
# the gate cannot bound (they live behind the OS fence, not a gate-side parser),
# so it declares the most-restrictive open-world destructive hints: NOT read-only
# and openWorldHint=True. This projects to NO catalog read/write tag (effectful/
# unclassifiable) â€” preserving the pre-#1061 "no read tag => never read-only, and
# not an fs-write the auto-edits mode may auto-approve" classification.


# POSIX text utilities the model tends to reach for (and improvises `wsl bash -c`
# to get on Windows, booting a resident VM â€” iowarp/clio-agent#898). Their real
# presence on the host PATH is probed, not assumed.
_POSIX_TEXT_TOOLS = ("cut", "sed", "awk", "grep")

# Operational caps â€” resolved file â†’ env â†’ default (see clio_agent.conf).
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
      heredoc profiling scripts). This is the CLIO input budget; Windows launchers
      can reject smaller commands, particularly cmd/.cmd/.bat at 8,191 characters.
      Quoting and wrapper arguments also count. Programs belong in workspace
      script files; the native launch string is checked separately.
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
    run outside the session's own workspace (iowarp/clio-agent â€” desktop
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
        from clio_agent.tools.write_scope import active_tool_write_root  # noqa: PLC0415

        active_root = str(active_tool_write_root() or get_active_tool_workspace_root())
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
    policy = FileAccessPolicy.from_env()
    policy._check_connected_storage(resolved, field="cwd")
    if not policy.allow_symlinks:
        from clio_agent.tools.file_policy import _has_symlink

        if _has_symlink(raw):
            raise FilePolicyError(
                code="symlink_denied",
                message=f"Symlinks are not allowed by file policy: {raw}",
                field="cwd",
                path=str(raw),
                next_action="Use a real directory path.",
            )
    if not policy.allows_source_read(resolved):
        policy._ensure_allowed(resolved, field="cwd")
    return resolved


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


def _shell_argv(command: str, *, prepared_environment: dict[str, str] | None = None) -> list[str]:
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
    if prepared_environment:
        from clio_agent.runtime.execution_environment import posix_command

        command = posix_command(command, prepared_environment)
    return [shell, "-lc", command]


@dataclass(frozen=True)
class ShellEnvFacts:
    """Host facts the shell tool description is computed from (#898).

    The tool description the model reads must describe the environment the
    command ACTUALLY executes in â€” the model improvised ``wsl bash -c "cut ..."``
    on Windows precisely because the tool called itself a *bash* and the host was
    not one. This is grounding, not a behavioural handcuff (CLAUDE.md âš‘ #3).
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
        f"Limits: CLIO accepts up to {limits.max_command_chars} command characters, but native "
        "shells/launchers can impose smaller bounds, even below this budget. "
        f"{SCRIPT_FILE_GUIDANCE} stdout and stderr each return inline up to "
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
    task_text = (
        "For remote commands, use ssh_execute when available with the shared SSH profile "
        "or jump route. Direct ssh in this local shell may be blocked by its network fence; "
        "approval/bypass does not disable that fence. "
        "For files outside permitted folders, declare access_paths and access_reason so CLIO "
        "asks the user before execution. Include source and destination for moves. "
        "Already approved downloaded source inputs can be read at their returned paths "
        "without access_paths. Those paths stay read-only; save derivatives in the workspace. "
        "By default background=false waits for command completion. Set background=true to "
        "return a durable Shell task handle as soon as the command starts, before completion. "
        "Continue independent work; use query_tasks, observe_tasks, wait_tasks, get_task_result "
        "or cancel_tasks with the handle. Results arrive at the next model iteration or wake "
        "you when idle. Conversation Stop leaves accepted background work running. A positive "
        "timeout_s remains an execution limit. Set timeout_s=0 for no per-command deadline; "
        "omitting it uses the configured default. An operator ceiling may still apply. "
    )
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
            "pipelines to reach cut/sed/awk/grep â€” on a WSL-less host they fail, and the "
            "first `wsl` call boots a utility VM that stays resident holding gigabytes. "
            "Paths use Windows conventions (drive letters, backslashes) unless the bash "
            "backend is active. For tabular, "
            "CSV, or columnar work (column selection, filtering, joins) use the pandas "
            "MCP tool when available instead of shell text pipelines — it is portable and "
            "spawns no VM. The working directory must be inside CLIO_ALLOWED_ROOTS "
            "or an approved downloaded source directory. "
            "A command can retrieve public HTTPS data when network policy permits; "
            "inspect its returned status and output before concluding access failed. "
            f"{task_text}{limits_text}"
        )
    return (
        f"Run ONE local shell command on a {facts.system_label} host and return stdout, "
        f"stderr, and exit code. Commands execute under {facts.shell_label}. "
        f"{tools_line} "
        "Paths use POSIX conventions (forward slashes). For large tabular, CSV, or "
        "columnar work (column selection, filtering, joins) prefer the pandas MCP tool "
        "when available over ad-hoc text pipelines. The working directory must be inside "
        "CLIO_ALLOWED_ROOTS or an approved downloaded source directory. "
        "A command can retrieve public HTTPS data when network policy permits; "
        "inspect its returned status and output before concluding access failed. "
        f"{task_text}{limits_text}"
    )


#: Computed once at server build from the running host (#898). The model reads
#: this as the ``bash`` tool description, so it is grounded in the real platform,
#: shell, and tool availability rather than a static "bash"-flavoured string.
_SHELL_TOOL_DESCRIPTION = build_shell_tool_description(_detect_shell_env())


def _log_shell_phase(
    call_id: str, phase: str, started: float, *, outcome: str = "completed"
) -> float:
    """Log wall time for one call stage without adding command contents to diagnostics."""
    finished = time.perf_counter()
    logger.info(
        "shell_phase call=%s phase=%s outcome=%s elapsed_ms=%.3f",
        call_id,
        phase,
        outcome,
        (finished - started) * 1000,
    )
    return finished


@shell_server.tool(description=_SHELL_TOOL_DESCRIPTION, annotations=_BASH_ANNOTATIONS)
async def bash(
    ctx: Context,
    command: str,
    cwd: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_output_bytes: int = _SHELL_LIMITS.default_output_bytes,
    background: Annotated[
        bool,
        Field(
            description="True returns a durable task handle before completion; false waits for exit."
        ),
    ] = False,
    access_paths: Annotated[
        list[str] | None,
        Field(
            description="Files or folders needing additional permission. Already approved downloaded source paths do not need this for reads and remain read-only. CLIO asks for folder access before running. Include both source and destination for a move."
        ),
    ] = None,
    access_reason: Annotated[
        str, Field(description="Explain why this command needs the declared external paths.")
    ] = "",
) -> dict[str, Any]:
    """Run one local shell command and return stdout, stderr, and exit code.

    The model-facing description is computed at server build from the host and
    the effective limits (:data:`_SHELL_TOOL_DESCRIPTION`, #898/#1487); this
    docstring is the developer reference. The command runs until it exits unless
    ``timeout_s`` is positive (or an operator ceiling applies) and the working
    directory must be inside ``CLIO_ALLOWED_ROOTS`` or an approved source copy.
    Output that does not fit the
    result budget is spilled in full under Agent-managed ``tool-output/`` state and excerpted
    (:mod:`clio_agent.tools.servers.shell_output`). Foreground turn cancellation
    kills its process tree; background tasks require explicit cancellation.
    """

    if background:
        return _error(
            "native_submission_required", "Background shell must use the native MCP task client."
        )
    return await execute_shell(
        ctx, command, cwd, timeout_s, max_output_bytes, access_paths=access_paths
    )


class ShellCleanupObserver(Protocol):
    """Attempt-specific evidence supplied to the native worker, never a task driver."""

    def acquiring_effects(self) -> None:
        """Mark process acquisition before the spawn can produce side effects."""

    def confirm_cleanup(self) -> None:
        """Confirm actual process-tree and reader retirement."""


async def execute_shell(
    ctx: ProgressReporter,
    command: str,
    cwd: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    max_output_bytes: int = _SHELL_LIMITS.default_output_bytes,
    *,
    cleanup: ShellCleanupObserver | None = None,
    access_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Execute the retained confined workload, shared by both native shell modes."""
    if not isinstance(command, str) or not command.strip():
        return _error(
            "invalid_command",
            "command must be a non-empty string.",
            details={"field": "command"},
        )
    if access_paths:
        # Declaring a path never grants it; execution rechecks actual folder rights.
        from clio_agent.gact.folder_access import requested_folders

        if requested_folders("shell_bash", {"access_paths": access_paths}):
            return _error("folder_access_required", "Ask the user to permit these folders first.")
    command = command.strip()
    max_chars = _SHELL_LIMITS.max_command_chars
    if len(command) > max_chars:
        return command_length_error(
            command,
            f"command is {len(command)} characters; the limit is {max_chars}.",
            bound="clio_input",
            max_chars=max_chars,
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
    call_id = new_call_id()
    phase_started = time.perf_counter()
    try:
        safe_cwd = _resolve_cwd(cwd)
        from clio_agent.runtime.execution_environment import shell_environment

        prepared = shell_environment(_spill_root(safe_cwd))
        argv = _shell_argv(command, prepared_environment=prepared)
    except FilePolicyError as exc:
        _log_shell_phase(call_id, "environment", phase_started, outcome="denied")
        return exc.to_result()
    except Exception as exc:  # noqa: BLE001
        _log_shell_phase(call_id, "environment", phase_started, outcome="failed")
        return _error("shell_unavailable", str(exc))
    phase_started = _log_shell_phase(call_id, "environment", phase_started)

    # #975: route the shell subprocess through the single confinement composer with the
    # per-invocation `shell` profile (write territory computed from THIS command's cwd).
    # Floor-first â€” the backend is passthrough this slice, so argv/env are byte-identical;
    # the shell seam carries no pdeathsig today, so it stays off here.
    from clio_agent.runtime import sandbox  # noqa: PLC0415 - avoid import cycle

    try:
        confined = sandbox.wrap_confined(
            argv[0],
            argv[1:],
            write_roots=sandbox.effective_write_roots(
                sandbox.PROFILE_SHELL, workspace_root=str(safe_cwd)
            ),
            net_policy=sandbox.NET_ALLOW_RECORD,
            profile=sandbox.PROFILE_SHELL,
            pdeathsig=False,
            cwd=safe_cwd,
        )
    except Exception:
        _log_shell_phase(call_id, "confinement", phase_started, outcome="failed")
        raise
    phase_started = _log_shell_phase(call_id, "confinement", phase_started)
    try:
        run_argv = [confined.command, *confined.args]
        if os.name == "nt" and (launch_error := windows_launch_error(run_argv, command)):
            return launch_error
        run_env = {**os.environ, **prepared, "PYTHONUTF8": "1", **confined.env_overlay}
        # The model's shell is not the authenticated human control channel.
        for internal_credential in ("CLIO_AUTH_TOKEN", "CLIO_GACT_BEARER_TOKEN"):
            run_env.pop(internal_credential, None)
        # Secrets meant for plugin servers (``${CLIO_SECRET_*}``) never reach the shell.
        run_env = {k: v for k, v in run_env.items() if not k.startswith("CLIO_SECRET_")}
        # Prepared scratch is already inside this workspace's writable territory.
        run_env.update({key: prepared[key] for key in ("TEMP", "TMP", "TMPDIR") if key in prepared})
        from clio_agent.paths import workspace_state_dir  # noqa: PLC0415

        # Skills run in isolated uv environments and cannot import this resolver.
        # Supply the bound workspace's canonical path, never an inherited host value
        # or an explicit command subdirectory. The existing write fence still applies.
        run_env["CLIO_AGENT_WORKSPACE_STATE_DIR"] = str(workspace_state_dir(_spill_root(safe_cwd)))

        spill_root = _spill_root(safe_cwd)
        spill_dir = spill_directory(spill_root, session_id=active_session_id())
        captures = [
            StreamCapture(
                name,
                inline_limit=max_output_bytes,
                spill_path=spill_dir / f"{call_id}.{name}.txt",
            )
            for name in ("stdout", "stderr")
        ]
        try:
            from clio_agent.runtime.task_process_tree import task_spawn_options

            process_options = task_spawn_options(confined.popen_kwargs)
            if cleanup is not None:
                cleanup.acquiring_effects()
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *run_argv,
                    cwd=str(safe_cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=run_env,
                    **process_options,
                    # Give the child an immediately-EOF stdin. Without this the spawned
                    # shell inherits clio-agent's own stdin (a pipe the parent holds open
                    # and never closes), and PowerShell/cmd block at startup waiting on
                    # that stream â€” every command then hits the timeout with empty output.
                    stdin=subprocess.DEVNULL,
                )
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # A cancelled submission waiter cannot abandon a newly created
                # suspended process. Resolve this exact spawn before stopping it.
                await _cancel_pending_spawn(spawn)
                if cleanup is not None:
                    cleanup.confirm_cleanup()
                raise
        except Exception as exc:  # noqa: BLE001
            if cleanup is not None:
                cleanup.confirm_cleanup()
            _log_shell_phase(call_id, "process_start", phase_started, outcome="failed")
            if is_command_length_exception(exc):
                return command_length_error(command, str(exc), bound="native_spawn")
            return _error("execution_failed", str(exc), details={"command": command})
        phase_started = _log_shell_phase(call_id, "process_start", phase_started)

        assert process.stdout is not None
        assert process.stderr is not None
        from clio_agent.runtime.task_process_tree import TaskProcessTree

        try:
            tree = TaskProcessTree(process.pid)
        except Exception:
            # Windows ownership failure kills the still-suspended process; wait
            # for its actual exit before allowing the worker to leave.
            if process.returncode is None:
                process.kill()
            await process.wait()

            raise
        readers = [
            asyncio.create_task(read_process_stream(pipe, capture=capture, ctx=ctx))
            for pipe, capture in zip((process.stdout, process.stderr), captures, strict=True)
        ]
        timed_out = False
        physical_settlement = False
        result: dict[str, Any] | None = None

        async def settle() -> None:
            await process.wait()
            while tree.active():
                await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(settle(), timeout=timeout or None)
            physical_settlement = True
        except TimeoutError:
            timed_out = True
            tree.terminate()
            await settle()
            physical_settlement = True
        except BaseException:
            _log_shell_phase(call_id, "child_lifetime", phase_started, outcome="interrupted")
            # Signal the owned OS tree directly; Core I/O may occupy the executor.
            # The receipt still follows actual process exit and output-reader retirement.
            tree.terminate()
            await settle()
            physical_settlement = True
            raise
        finally:
            phase_started = _log_shell_phase(
                call_id,
                "child_lifetime",
                phase_started,
                outcome="timed_out"
                if timed_out
                else "completed"
                if physical_settlement
                else "unsettled",
            )
            try:
                await asyncio.gather(*readers)
                _log_shell_phase(call_id, "output_drain", phase_started)
            finally:
                tree.close()
                if physical_settlement:
                    if cleanup is not None and all(reader.done() for reader in readers):
                        # A reader failure can leave output custody unconfirmed.
                        if all(
                            not reader.cancelled() and reader.exception() is None
                            for reader in readers
                        ):
                            cleanup.confirm_cleanup()
                    base = {
                        "command": command,
                        "cwd": str(safe_cwd),
                        "exit_code": None if timed_out else process.returncode,
                        "timed_out": timed_out,
                        "timeout_s": timeout,
                    }
                    result = compose_output_fields(
                        base, captures, root=spill_root, budget=shell_result_char_budget()
                    )
        if result is None:
            raise RuntimeError("Shell exited without confirmed process-tree settlement")
        return result
    finally:
        # The child has exited (or never started): its egress channel goes with it.
        confined.release()


async def _cancel_pending_spawn(spawn: asyncio.Task[asyncio.subprocess.Process]) -> None:
    """Resolve an exact spawn race and physically settle it without replaying the command."""
    from clio_agent.runtime.task_process_tree import TaskProcessTree

    async def cleanup() -> None:
        process = await spawn
        tree = None
        try:
            tree = TaskProcessTree(process.pid)
            tree.terminate()
            await process.wait()
            while tree.active():
                await asyncio.sleep(0.05)
        finally:
            if tree is not None:
                tree.close()
            elif process.returncode is None:
                process.kill()
                await process.wait()

    pending = asyncio.create_task(cleanup())
    while True:
        try:
            await asyncio.shield(pending)
            return
        except asyncio.CancelledError:
            if pending.cancelled():
                raise


__all__ = [
    "SHELL_TOOL_ANNOTATIONS",
    "ShellEnvFacts",
    "ShellLimits",
    "build_shell_tool_description",
    "resolve_shell_limits",
    "shell_server",
]
