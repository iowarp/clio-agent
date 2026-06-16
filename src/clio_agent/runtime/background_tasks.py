"""Background task primitives: spawn → handle, status/poll, wait_for, cancel, notify.

The monitor/wait_for substrate for async expert execution (epic #667, issue #670),
modeled on how Claude Code surfaces background work. A unit of background work (an
expert turn, a long tool call) gets a stable handle at spawn; the orchestrator then

  * ``status`` / ``poll_output`` — pull mode (where is it, what has it produced)
  * ``wait`` — block until done OR a predicate holds, with a timeout
  * ``on_complete`` — push mode (notified when it finishes)
  * ``cancel`` — kill by handle

without the parent losing the thread. This layer is transport- and domain-agnostic:
it runs coroutines and tracks them. Wiring to expert delegation and the event bus
lives in the caller, so the primitive stays unit-testable on its own.

Principle (CLAUDE.md): the completion *event* feeds the parent model's decision;
this layer carries status/result/output and never decides "done enough" itself.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional


class TaskStatus(str, Enum):
    """Lifecycle of a background task (mirrors issue #441's status set)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


class OutputSink:
    """Handed to the running work; appends incremental output to its record.

    Large/streamed output belongs in an artifact the handle points at, not the
    prompt — this sink is for short progress lines the orchestrator can poll.
    """

    def __init__(self, record: "TaskRecord") -> None:
        self._record = record

    def emit(self, line: str) -> None:
        self._record.output.append(str(line))


@dataclass
class TaskRecord:
    """The handle's state. ``result``/``error`` are set once terminal."""

    id: str
    label: str
    status: TaskStatus = TaskStatus.QUEUED
    result: Any = None
    error: Optional[str] = None
    output: list[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def snapshot(self) -> dict[str, Any]:
        """A redactable view for a status poll / SSE frame (never the raw result)."""
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status.value,
            "error": self.error,
            "output_lines": len(self.output),
        }


Work = Callable[[OutputSink], Awaitable[Any]]


class BackgroundTasks:
    """asyncio-backed registry implementing the monitor/wait_for primitives."""

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._on_complete: dict[str, list[Callable[[TaskRecord], None]]] = {}

    def spawn(self, work: Work, *, label: str = "") -> str:
        """Launch ``work`` in the background; return its handle id immediately.

        ``work`` is a coroutine factory taking an :class:`OutputSink`. Exceptions
        are recorded on the handle (status FAILED), never raised into the loop.
        """
        tid = f"bt_{uuid.uuid4().hex[:12]}"
        rec = TaskRecord(id=tid, label=label or tid)
        self._records[tid] = rec
        sink = OutputSink(rec)

        async def _runner() -> None:
            rec.status = TaskStatus.RUNNING
            try:
                rec.result = await work(sink)
                rec.status = TaskStatus.COMPLETED
            except asyncio.CancelledError:
                rec.status = TaskStatus.CANCELLED
                raise
            except Exception as exc:  # noqa: BLE001 — recorded on the handle, not raised
                rec.status = TaskStatus.FAILED
                rec.error = f"{type(exc).__name__}: {exc}"
            finally:
                rec.done.set()
                for cb in tuple(self._on_complete.get(tid, ())):
                    try:
                        cb(rec)
                    except Exception:  # noqa: BLE001 — a bad notifier can't break completion
                        pass

        rec.task = asyncio.ensure_future(_runner())
        return tid

    def get(self, tid: str) -> Optional[TaskRecord]:
        return self._records.get(tid)

    def status(self, tid: str) -> TaskStatus:
        return self._require(tid).status

    def poll_output(self, tid: str, *, since: int = 0) -> list[str]:
        """Incremental output since line index ``since`` (pull mode)."""
        return self._require(tid).output[since:]

    def list(self) -> list[dict[str, Any]]:
        return [r.snapshot() for r in self._records.values()]

    def on_complete(self, tid: str, callback: Callable[[TaskRecord], None]) -> None:
        """Register a completion notifier (push mode). Fires immediately if already
        terminal. The callback must not block — it's for an event frame or a side
        effect (logging/metrics), not control flow."""
        rec = self._require(tid)
        if rec.status.terminal:
            callback(rec)
            return
        self._on_complete.setdefault(tid, []).append(callback)

    async def wait(
        self,
        tid: str,
        *,
        until: Optional[Callable[[TaskRecord], bool]] = None,
        timeout: Optional[float] = None,
    ) -> TaskRecord:
        """Block until the task completes (``until=None``) or ``until(record)`` holds,
        whichever comes first, bounded by ``timeout`` seconds. Returns the record
        regardless — inspect ``.status`` to tell completion from a timeout/condition.
        A wait without a timeout can hang, so callers should pass one."""
        rec = self._require(tid)
        if until is None:
            try:
                await asyncio.wait_for(rec.done.wait(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            return rec

        async def _poll() -> None:
            while not (rec.done.is_set() or until(rec)):
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(_poll(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        return rec

    def cancel(self, tid: str) -> bool:
        """Request cancellation. Returns whether a cancel was delivered; the status
        settles to CANCELLED once the task unwinds (await the handle to observe it)."""
        rec = self._require(tid)
        if rec.task is not None and not rec.task.done():
            return rec.task.cancel()
        return False

    def remove(self, tid: str) -> bool:
        """Drop a terminal task's record to bound memory. Returns whether it existed.
        Refuses to drop a still-running task (cancel it first)."""
        rec = self._records.get(tid)
        if rec is None:
            return False
        if not rec.status.terminal:
            raise ValueError(f"task {tid} is {rec.status.value}; cancel before removing")
        self._records.pop(tid, None)
        self._on_complete.pop(tid, None)
        return True

    def prune(self) -> int:
        """Evict every terminal record (bounds unbounded growth in a long-running
        registry). Returns how many were dropped; running tasks are kept."""
        terminal = [tid for tid, rec in self._records.items() if rec.status.terminal]
        for tid in terminal:
            self._records.pop(tid, None)
            self._on_complete.pop(tid, None)
        return len(terminal)

    def _require(self, tid: str) -> TaskRecord:
        rec = self._records.get(tid)
        if rec is None:
            raise KeyError(f"unknown background task: {tid}")
        return rec


async def _run_command(
    sink: OutputSink, command: str, *, cwd: Optional[str] = None, timeout: Optional[float] = None
) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def _drain() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            sink.emit(raw.decode("utf-8", "replace").rstrip("\n"))

    try:
        await asyncio.wait_for(asyncio.gather(_drain(), proc.wait()), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        await proc.wait()
        return {"exit_code": None, "timed_out": True}
    return {"exit_code": proc.returncode, "timed_out": False}


def spawn_command(
    tasks: BackgroundTasks,
    command: str,
    *,
    label: str = "",
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """Run a shell command as a monitored background task (the Bash(run_in_background)
    shape): stdout/stderr lines stream to the handle's ``poll_output`` and the result is
    ``{exit_code, timed_out}``. Same handle/monitor/wait/cancel contract as an expert
    delegation — so an expert can spawn a long-running command and wait on it the same
    way it waits on another expert."""
    return tasks.spawn(
        lambda sink: _run_command(sink, command, cwd=cwd, timeout=timeout),
        label=label or "cmd",
    )
