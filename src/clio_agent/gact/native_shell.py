"""Register the retained shell workload on its independent native FastMCP root."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastmcp import Context
from fastmcp.utilities.tasks import TaskConfig

from clio_agent.arc.task_types import SubmissionContext
from clio_agent.gact.native_progress import NativeProgress
from clio_agent.gact.native_work import NativeWork, WorkEvidence
from clio_agent.tools.native_roots import NativeRoot
from clio_agent.tools.servers.shell_server import (
    _BASH_ANNOTATIONS,
    _DEFAULT_TIMEOUT_S,
    _SHELL_LIMITS,
    _SHELL_TOOL_DESCRIPTION,
    execute_shell,
)


def shell_poll_interval() -> timedelta:
    """The status-poll ceiling a shell task advertises (``tools.shell_poll_interval_ms``).

    The client's backoff starts at 20 ms and doubles up to it, so a command is seen as
    done at most this long after it ends; fastmcp's 5 s default left a median 375 ms
    (L1P/L2, 2026-11-04). Unchanged replies are not recorded (``native_task_replies``).
    """
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "tools.shell_poll_interval_ms",
        env="CLIO_TOOLS_SHELL_POLL_INTERVAL_MS",
        default=200,
        cast=conf.as_int,
    )
    return timedelta(milliseconds=max(20, value))


def register_native_shell(root: NativeRoot, work: NativeWork) -> None:
    """Require native execution in both foreground and background modes."""

    @root.tool(
        name="bash",
        description=_SHELL_TOOL_DESCRIPTION,
        annotations=_BASH_ANNOTATIONS,
        task=TaskConfig(mode="required", poll_interval=shell_poll_interval()),
    )
    async def native_bash(
        ctx: Context,
        command: str,
        _clio_submission: str,
        cwd: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_output_bytes: int = _SHELL_LIMITS.default_output_bytes,
        background: bool = False,
        access_paths: list[str] | None = None,
        access_reason: str = "",
    ) -> dict[str, Any]:
        async def body(submission: SubmissionContext, evidence: WorkEvidence) -> dict[str, Any]:
            return await execute_shell(
                NativeProgress(submission, work, ctx),
                command,
                cwd,
                timeout_s,
                max_output_bytes,
                cleanup=evidence,
                access_paths=access_paths,
            )

        return await work.run(
            _clio_submission,
            "bash",
            {
                "command": command,
                "cwd": cwd,
                "timeout_s": timeout_s,
                "max_output_bytes": max_output_bytes,
                "background": background,
                "access_paths": access_paths,
                "access_reason": access_reason,
            },
            body,
        )
