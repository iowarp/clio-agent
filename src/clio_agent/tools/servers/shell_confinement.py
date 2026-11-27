"""How one shell command is confined: CLIO's sandbox, or outside it with a person's allow.

:func:`clio_agent.tools.servers.shell_server.execute_shell` routes every command through
the single confinement composer with the per-invocation ``shell`` profile (#975). A
command that asks to run ``outside_sandbox`` runs unfenced only with a person's allow for
exactly that command (:mod:`clio_agent.runtime.sandbox_outside`); without one it is
refused (``outside_sandbox_not_approved``), and its declared ``access_paths`` are not
folder requests, since the fence does not apply to it.

The shell's own error shape and logger stay in :mod:`shell_server`, reached at call time,
so this owner module adds no import cycle.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.runtime.sandbox import ConfinedSpawn

logger = logging.getLogger(__name__)


def folder_refusal(access_paths: list[str] | None, outside_sandbox: bool) -> dict[str, Any] | None:
    """Refuse a fenced command whose declared paths are not yet permitted folders."""
    if access_paths and not outside_sandbox:
        # Declaring a path never grants it; execution rechecks actual folder rights.
        from clio_agent.gact.folder_access import requested_folders  # noqa: PLC0415
        from clio_agent.tools.servers.shell_server import _error  # noqa: PLC0415

        if requested_folders("shell_bash", {"access_paths": access_paths}):
            return _error("folder_access_required", "Ask the user to permit these folders first.")
    return None


def outside_refusal(command: str, call_id: str) -> dict[str, Any] | None:
    """Use up the person's allow for exactly this command; the refusal when there is none."""
    from clio_agent.runtime import sandbox_outside  # noqa: PLC0415
    from clio_agent.tools.servers import shell_server  # noqa: PLC0415 - avoid import cycle

    if not sandbox_outside.take(command):
        return shell_server._error(
            sandbox_outside.REASON_NOT_APPROVED,
            "Running outside the sandbox needs the person's allow for this exact command. "
            "Call shell_bash with outside_sandbox and access_reason; CLIO asks them.",
        )
    shell_server.logger.warning(
        "shell command running outside the sandbox with the person's allow call=%s", call_id
    )
    return None


async def confine_off_loop(
    argv: list[str],
    safe_cwd: Path,
    *,
    outside_sandbox: bool,
    temp: dict[str, str] | None = None,
) -> "ConfinedSpawn":
    """:func:`confine` on a worker thread, in a copy of this call's context.

    The shell runs on the app loop, which every other call's exit callbacks, timers and
    permission answers share. Composing a fence can block for seconds on a cold host (the
    first one asks the person's PowerShell for its execution policy), which held all of
    them. When the call is cancelled meanwhile, the spawn the thread composes is released,
    so its egress channel does not outlive the call.
    """
    work = asyncio.ensure_future(
        asyncio.to_thread(confine, argv, safe_cwd, outside_sandbox=outside_sandbox, temp=temp)
    )
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        work.add_done_callback(_release_abandoned)
        raise


def _release_abandoned(work: "asyncio.Future[ConfinedSpawn]") -> None:
    if work.cancelled():
        return
    if (error := work.exception()) is not None:
        logger.warning(
            "shell confinement failed after its call was cancelled "
            "reason=confinement_failed_after_cancel error=%s: %s",
            type(error).__name__,
            error,
        )
        return
    asyncio.get_running_loop().run_in_executor(None, work.result().release)


def confine(
    argv: list[str],
    safe_cwd: Path,
    *,
    outside_sandbox: bool,
    temp: dict[str, str] | None = None,
) -> "ConfinedSpawn":
    """The spawn for ``argv``: fenced to the ``shell`` profile, or unfenced once allowed.

    Floor-first: when the backend is passthrough, argv/env are byte-identical; the shell
    seam carries no pdeathsig today, so it stays off here. ``temp`` is the session scratch
    ``TEMP``/``TMP``, kept inside Windows' MXC container, which would replace them.
    """
    from clio_agent.runtime import sandbox  # noqa: PLC0415 - avoid import cycle
    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415

    if outside_sandbox:
        return sandbox.ConfinedSpawn(
            command=argv[0],
            args=list(argv[1:]),
            env_overlay={},
            popen_kwargs={},
            result=sandbox.SandboxResult(
                mechanism=sandbox.MECHANISM_NONE,
                active=False,
                reason="outside_sandbox_allowed",
            ),
        )
    return sandbox.wrap_confined(
        argv[0],
        argv[1:],
        write_roots=sandbox.effective_write_roots(
            sandbox.PROFILE_SHELL, workspace_root=str(safe_cwd)
        ),
        net_policy=sandbox.NET_ALLOW_RECORD,
        profile=sandbox.PROFILE_SHELL,
        pdeathsig=False,
        cwd=safe_cwd,
        egress_root=get_active_tool_workspace_root() or safe_cwd,
        container_env=temp,
    )
