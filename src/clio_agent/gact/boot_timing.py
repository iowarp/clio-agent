"""When each startup step ran, measured inside the server (owner module).

Startup is judged by how long each step takes on a long-lived store, so the server
records its own steps instead of relying on an external profiler. Every step has a
name, the thread it ran on, its start and end in seconds since the process started,
and its outcome (``ok`` or the exception type). Marks record single moments such as
``serving`` or ``agent_ready``.

The timeline is kept on ``app.state.boot_timeline`` and is read by ``GET /v1/health``
(its ``boot`` block) and written to the trace under the ``BOOT`` tag as each step ends.
It records the startup of this process only: steps and marks after the first are
appended, never reset.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from clio_agent.runtime import trace

T = TypeVar("T")


def _process_started_at() -> float:
    """Wall-clock time this process started, so steps are measured from launch."""
    import psutil  # noqa: PLC0415 - only needed once per process

    return float(psutil.Process().create_time())


@dataclass(frozen=True)
class BootStep:
    """One finished startup step."""

    name: str
    thread: str
    start_s: float
    end_s: float
    outcome: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "thread": self.thread,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "seconds": round(self.end_s - self.start_s, 3),
            "outcome": self.outcome,
        }


class BootTimeline:
    """Thread-safe record of startup steps, running steps and marks."""

    def __init__(self, started_at: float | None = None) -> None:
        self._origin = _process_started_at() if started_at is None else started_at
        self._lock = threading.Lock()
        self._steps: list[BootStep] = []
        self._running: dict[int, tuple[str, str, float]] = {}
        self._marks: dict[str, float] = {}
        self._next = 0

    def now(self) -> float:
        """Seconds since the process started."""
        return round(time.time() - self._origin, 3)

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Time the enclosed block as the step ``name``."""
        key = self._begin(name)
        outcome = "ok"
        try:
            yield
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            self._end(key, outcome)

    async def timed(self, name: str, awaitable: Awaitable[T]) -> T:
        """Await ``awaitable`` as the step ``name`` and return its result."""
        with self.step(name):
            return await awaitable

    def mark(self, name: str) -> None:
        """Record the first time ``name`` happened."""
        at = self.now()
        with self._lock:
            if name in self._marks:
                return
            self._marks[name] = at
        trace.event("BOOT", "mark=%s at=%.3f", name, at)

    def snapshot(self) -> dict[str, Any]:
        """The timeline as plain data: finished steps, running steps and marks."""
        now = self.now()
        with self._lock:
            steps = [step.as_dict() for step in self._steps]
            running = [
                {"name": name, "thread": thread, "start_s": start, "seconds": round(now - start, 3)}
                for name, thread, start in self._running.values()
            ]
            marks = dict(self._marks)
        return {"steps": steps, "running": running, "marks": marks}

    def _begin(self, name: str) -> int:
        start = self.now()
        with self._lock:
            key = self._next
            self._next += 1
            self._running[key] = (name, threading.current_thread().name, start)
        return key

    def _end(self, key: int, outcome: str) -> None:
        end = self.now()
        with self._lock:
            name, thread, start = self._running.pop(key)
            step = BootStep(name, thread, start, end, outcome)
            self._steps.append(step)
        trace.event(
            "BOOT",
            "step=%s seconds=%.3f start=%.3f thread=%s outcome=%s",
            name,
            end - start,
            start,
            thread,
            outcome,
        )


_CREATE = threading.Lock()


def timeline(app: Any) -> BootTimeline:
    """The app's timeline, created on first use (in-process test apps have no lifespan)."""
    found = getattr(app.state, "boot_timeline", None)
    if found is not None:
        return found
    with _CREATE:
        found = getattr(app.state, "boot_timeline", None)
        if found is None:
            found = BootTimeline()
            app.state.boot_timeline = found
    return found


def step(app: Any, name: str) -> Any:
    """Context manager timing the enclosed block as the startup step ``name``."""
    return timeline(app).step(name)


async def timed(app: Any, name: str, awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` as the startup step ``name``."""
    return await timeline(app).timed(name, awaitable)


def mark(app: Any, name: str) -> None:
    """Record the moment ``name`` first happened during startup."""
    timeline(app).mark(name)


def snapshot(app: Any) -> dict[str, Any]:
    """The ``boot`` block served by ``GET /v1/health``."""
    return timeline(app).snapshot()
