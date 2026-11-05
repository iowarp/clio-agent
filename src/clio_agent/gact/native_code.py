"""Background ``run_code``: a script as owned work on the code native root (taskable CT2).

``run_code(background=True)`` approves the script as a synchronous call does, then hands
it to this root and returns at acceptance with the task's handle. The worker runs it in
a kernel of its own (keyed by the task's handle, so the scope's kernel stays free), off
the event loop, until the script finishes or the task is cancelled. Nothing else ends it
(CT3). Its nested calls run in the context the launching ``run_code`` call captured: its
grant, its parent call id and its session.

What cannot cross the task boundary as JSON (the agent's tools and that context) passes
through :data:`_LAUNCHES`: staged by ``run_code`` under a fresh key, the key sent as the
task's argument, and taken by the worker when it starts. It is a hand-off, not a registry
of running scripts: an entry lives from approval to the worker's start. After a restart
the entry is gone, so a redelivered task fails with ``code_task_launch_missing`` rather
than running without its approval.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastmcp import Context
from fastmcp.utilities.tasks import TaskConfig

from clio_agent.arc.task_types import SubmissionContext
from clio_agent.gact.native_work import NativeWork, WorkEvidence
from clio_agent.tools.native_roots import NativeRoot

__all__ = [
    "CODE_TASK_LAUNCH_MISSING",
    "CODE_TASK_TOOL",
    "CodeLaunch",
    "register_native_code",
    "stage_launch",
    "take_launch",
    "staged",
]

#: The worker's tool name on the code root.
CODE_TASK_TOOL = "run_code_task"
#: Typed failure: the worker found no staged launch (a restart, or a forged key).
CODE_TASK_LAUNCH_MISSING = "code_task_launch_missing"


@dataclass(frozen=True)
class CodeLaunch:
    """What a background script needs that is not JSON: its prepared script and kernel."""

    prepared: Any  # code_mode.tool.PreparedScript
    session_id: str
    workspace: Path
    read_only: bool


_LAUNCHES: dict[str, CodeLaunch] = {}
_LAUNCHES_LOCK = threading.Lock()


def stage_launch(launch: CodeLaunch) -> str:
    """Stage an approved script for the worker; the key its task carries."""
    key = "launch_" + uuid4().hex
    with _LAUNCHES_LOCK:
        _LAUNCHES[key] = launch
    return key


def staged(key: str) -> bool:
    """Whether ``key`` names an approved script not yet taken by a worker."""
    with _LAUNCHES_LOCK:
        return key in _LAUNCHES


def take_launch(key: str) -> CodeLaunch | None:
    """Take a staged launch (the worker at its start, or a launch that was never accepted)."""
    with _LAUNCHES_LOCK:
        return _LAUNCHES.pop(key, None)


def _run(launch: CodeLaunch, handle: str, stop: threading.Event) -> dict[str, Any]:
    """Run the script in its own kernel until it ends or ``stop`` is set; stop the kernel."""
    from clio_agent.gact.code_mode import kernels  # noqa: PLC0415
    from clio_agent.gact.code_mode.tool import execute_script  # noqa: PLC0415

    kernel = kernels.kernel_for(
        launch.session_id, "task:" + handle, launch.workspace, read_only=launch.read_only
    )
    try:
        return execute_script(launch.prepared, kernel, stop.is_set)
    finally:
        kernels.discard(kernel, "task_settled")


def register_native_code(root: NativeRoot, work: NativeWork) -> None:
    """Register the background script worker on the code root."""
    from clio_agent.gact.native_shell import shell_poll_interval  # noqa: PLC0415

    @root.tool(
        name=CODE_TASK_TOOL,
        description="Run an approved Python script as a background task.",
        task=TaskConfig(mode="required", poll_interval=shell_poll_interval()),
    )
    async def native_run_code(
        ctx: Context,
        code: str,
        launch: str,
        _clio_submission: str,
        label: str = "",
    ) -> dict[str, Any]:
        async def body(submission: SubmissionContext, evidence: WorkEvidence) -> dict[str, Any]:
            from clio_agent.gact.code_mode.tool import model_text  # noqa: PLC0415

            staged_launch = take_launch(launch)
            if staged_launch is None or staged_launch.prepared.code != code:
                raise RuntimeError(
                    f"{CODE_TASK_LAUNCH_MISSING}: this script has no approved launch in this "
                    "server process (it does not survive a restart); run it again."
                )
            stop = threading.Event()
            running = asyncio.ensure_future(
                asyncio.to_thread(_run, staged_launch, submission.handle, stop)
            )
            try:
                outcome = await asyncio.shield(running)
            except asyncio.CancelledError:
                stop.set()  # the script's loop sees it at its next poll and stops the kernel
                await asyncio.shield(running)
                raise
            return {
                "status": outcome["status"],
                "label": label,
                "calls": outcome.get("calls", 0),
                "failed_calls": outcome.get("failed_calls", 0),
                "text": model_text(outcome),
            }

        return await work.run(
            _clio_submission, CODE_TASK_TOOL, {"code": code, "launch": launch, "label": label}, body
        )
