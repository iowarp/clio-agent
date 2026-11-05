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
from clio_agent.gact.code_task_context import (
    _BACKGROUND_TASK,
    BackgroundTask,
    active_background_task,
    question_cancel_token,
)
from clio_agent.gact.native_work import NativeWork, WorkEvidence
from clio_agent.tools.native_roots import NativeRoot

__all__ = [
    "CODE_TASK_LAUNCH_MISSING",
    "CODE_TASK_TOOL",
    "BackgroundTask",
    "CodeLaunch",
    "active_background_task",
    "question_cancel_token",
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


def _duration(seconds: float) -> str:
    whole = int(seconds)
    if whole < 60:
        return f"{whole} s"
    if whole < 3600:
        return f"{whole // 60} min {whole % 60} s"
    return f"{whole // 3600} h {whole % 3600 // 60} min"


def status_summary(row: dict[str, Any]) -> str:
    """One line for the task row: elapsed time, calls, memory, and the last progress."""
    calls = int(row.get("calls") or 0)
    parts = [
        _duration(float(row.get("elapsed_s") or 0)),
        f"{calls:,} call{'' if calls == 1 else 's'}",
    ]
    if row.get("memory_mb") is not None:
        parts.append(f"{float(row['memory_mb']):.0f} MB")
    done, total = row.get("done"), row.get("total")
    if done is not None:
        parts.append(f"{done} of {total}" if total is not None else f"{done} done")
    text = ", ".join(parts)
    message = str(row.get("message") or "").strip()
    return f"{text}: {message}" if message else text


def status(live: dict[str, Any], started: float) -> dict[str, Any]:
    """The task row's view of a running script: elapsed time, nested calls, memory and
    its last ``task.progress`` (kept between status reports)."""
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    dispatcher, kernel = live.get("dispatcher"), live.get("kernel")
    process = getattr(kernel, "process", None)
    path = str(live.get("output_path") or "")
    row = {
        "type": "clio.code.status",
        "state": "running",
        "elapsed_s": round(time.monotonic() - started, 1),
        "calls": len(getattr(dispatcher, "records", ()) or ()),
        "memory_mb": _memory_mb(getattr(process, "pid", None)),
        "output_path": path,
        "output_bytes": os.path.getsize(path) if path and os.path.exists(path) else 0,
        **(live.get("last_progress") or {}),
    }
    row["summary"] = status_summary(row)
    return row


def _run(
    launch: CodeLaunch, handle: str, stop: threading.Event, live: dict[str, Any]
) -> dict[str, Any]:
    """Run the script in its own kernel until it ends or ``stop`` is set; stop the kernel.

    The kernel gets the ``task`` binding, and the script's dispatcher answers its calls
    (:mod:`clio_agent.gact.code_task_reporting`)."""
    from clio_agent.gact import code_task_reporting as reporting  # noqa: PLC0415
    from clio_agent.gact import context as gact_context  # noqa: PLC0415
    from clio_agent.gact.code_mode import kernels  # noqa: PLC0415
    from clio_agent.gact.code_mode.tool import execute_script  # noqa: PLC0415

    kernel = kernels.kernel_for(
        launch.session_id, "task:" + handle, launch.workspace, read_only=launch.read_only
    )
    live["kernel"] = kernel
    from clio_agent.gact.code_task_records import CallGrouper  # noqa: PLC0415
    from clio_agent.tools.tool_observation import OBSERVATION_GROUP  # noqa: PLC0415

    task = BackgroundTask(handle, stop)
    grouper = CallGrouper(asked=lambda: task.questions[0])
    launch.prepared.context.run(_BACKGROUND_TASK.set, task)
    launch.prepared.context.run(OBSERVATION_GROUP.set, grouper)
    app = launch.prepared.context.run(gact_context.active_app)
    reserved = reporting.handlers(app, launch.session_id, handle, live.get("label", ""), live)
    try:
        return execute_script(
            launch.prepared,
            kernel,
            stop.is_set,
            live,
            bindings=reporting.binding(),
            reserved=reserved,
            output_path=str(live.get("output_path") or ""),
        )
    finally:
        grouper.flush()  # what was held is recorded when the script ends
        kernels.discard(kernel, "task_settled")
        from clio_agent.gact.streams.store import stream_store  # noqa: PLC0415

        stream_store(app).stop_task(handle)  # its streams stop, their last rows kept


def _on_own_thread(handle: str, run: Any) -> asyncio.Future[Any]:
    """Run a background script on a thread of its own, its outcome a future on this loop.

    Not the loop's default executor: a script holds its thread for as long as it runs,
    and its nested calls need that executor's threads (admission, task history), so a
    few long scripts would starve every script's calls (CK3: 8 threads, 30 scripts,
    every call stalled). Scripts have no count limit (CT18), so each gets its own thread.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def settle(outcome: Any, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(outcome)

    def target() -> None:
        try:
            outcome = run()
        except BaseException as exc:  # noqa: BLE001 - forwards: the task's awaiting future
            loop.call_soon_threadsafe(settle, None, exc)
        else:
            loop.call_soon_threadsafe(settle, outcome, None)

    threading.Thread(target=target, name=f"code-task-{handle}", daemon=True).start()
    return future


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
            from clio_agent.tools.mcp_result_projection import (  # noqa: PLC0415
                bounded_model_tool_result,
            )

            staged_launch = take_launch(launch)
            if staged_launch is None or staged_launch.prepared.code != code:
                raise RuntimeError(
                    f"{CODE_TASK_LAUNCH_MISSING}: this script has no approved launch in this "
                    "server process (it does not survive a restart); run it again."
                )
            import json  # noqa: PLC0415
            import time  # noqa: PLC0415

            from clio_agent.gact.native_progress import NativeProgress  # noqa: PLC0415

            stop, started = threading.Event(), time.monotonic()
            progress = NativeProgress(submission, work, ctx)
            from clio_agent.runtime.session_scratch import session_scratch_dir  # noqa: PLC0415

            scratch = session_scratch_dir(submission.owner.session_id)
            live: dict[str, Any] = {
                "output_path": str(scratch / f"code-task-{submission.handle}.log")
                if scratch is not None
                else "",
                "label": label,
                "started": started,
                "progress": progress,
                "loop": asyncio.get_running_loop(),
            }
            running = _on_own_thread(
                submission.handle, lambda: _run(staged_launch, submission.handle, stop, live)
            )

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
                "notifies_coalesced": int(live.get("coalesced") or 0),
                "output_path": str(live.get("output_path") or ""),
                # The caller's room bounds it; past it, the cut-result note names the file.
                "text": bounded_model_tool_result(model_text(outcome)),
            }

        return await work.run(
            _clio_submission, CODE_TASK_TOOL, {"code": code, "launch": launch, "label": label}, body
        )
