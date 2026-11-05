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


def status_interval_s() -> float:
    """Seconds between a running script's status reports (``code.background_status_s``):
    a display cadence for the task row, not a limit."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "code.background_status_s", env="CLIO_CODE_BACKGROUND_STATUS_S", default=5.0, cast=float
    )
    return max(0.5, value)


def _memory_mb(pid: int | None) -> float | None:
    """Resident memory of the kernel and its children, in MB; ``None`` when unreadable."""
    if not pid:
        return None
    import psutil  # noqa: PLC0415

    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
        total = 0
        for process in processes:
            try:
                total += process.memory_info().rss
            except psutil.Error:
                continue
        return round(total / 1e6, 1)
    except psutil.Error:
        return None


def status(live: dict[str, Any], started: float) -> dict[str, Any]:
    """The task row's view of a running script: elapsed time, nested calls, memory."""
    import time  # noqa: PLC0415

    dispatcher, kernel = live.get("dispatcher"), live.get("kernel")
    process = getattr(kernel, "process", None)
    return {
        "type": "clio.code.status",
        "state": "running",
        "elapsed_s": round(time.monotonic() - started, 1),
        "calls": len(getattr(dispatcher, "records", ()) or ()),
        "memory_mb": _memory_mb(getattr(process, "pid", None)),
    }


def _run(
    launch: CodeLaunch, handle: str, stop: threading.Event, live: dict[str, Any]
) -> dict[str, Any]:
    """Run the script in its own kernel until it ends or ``stop`` is set; stop the kernel."""
    from clio_agent.gact.code_mode import kernels  # noqa: PLC0415
    from clio_agent.gact.code_mode.tool import execute_script  # noqa: PLC0415

    kernel = kernels.kernel_for(
        launch.session_id, "task:" + handle, launch.workspace, read_only=launch.read_only
    )
    live["kernel"] = kernel
    try:
        return execute_script(launch.prepared, kernel, stop.is_set, live)
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
            import json  # noqa: PLC0415
            import time  # noqa: PLC0415

            from clio_agent.gact.native_progress import NativeProgress  # noqa: PLC0415

            live: dict[str, Any] = {}
            stop, started = threading.Event(), time.monotonic()
            running = asyncio.ensure_future(
                asyncio.to_thread(_run, staged_launch, submission.handle, stop, live)
            )
            progress = NativeProgress(submission, work, ctx)

            async def report() -> None:
                interval = status_interval_s()
                while not running.done():
                    await asyncio.sleep(interval)
                    if not running.done():
                        row = status(live, started)
                        await progress.report_progress(row["elapsed_s"], None, json.dumps(row))

            reporting = asyncio.ensure_future(report())
            try:
                outcome = await asyncio.shield(running)
            except asyncio.CancelledError:
                stop.set()  # the script's loop sees it at its next poll and stops the kernel
                await asyncio.shield(running)
                raise
            finally:
                reporting.cancel()
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
