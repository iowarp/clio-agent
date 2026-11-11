"""CLIO's own time in one agent step, by part, as a stream-audit row.

X2 showed 0.06-0.5 s between a tool result and the next model call with the highway
flush only 46 ms a run; this names where the rest goes: ``prep`` (arrivals, tool
refresh, compaction check, working state), ``flush``, ``commit`` (input commit),
``build`` (reading the context into the request) and ``record`` (the step's writes).
Code the step calls marks its own parts on the step's clock with :func:`mark`
(``record_write``, ``record_collect``, ``record_progress``, ``record_event``).
"""

from __future__ import annotations

from contextvars import ContextVar

__all__ = ["StepClock", "mark"]

_ACTIVE: ContextVar[StepClock | None] = ContextVar("clio_step_clock", default=None)


def mark(name: str) -> None:
    """Mark ``name`` on the running step's clock; nothing outside a step."""
    clock = _ACTIVE.get()
    if clock is not None:
        clock.mark(name)


class StepClock:
    """CLIO's own time in one step, by part (``react.step.overhead`` stream-audit row).

    ``mark(name)`` adds the time since the previous mark to ``name`` (an empty name
    only restarts the clock, across the model call and the tools)."""

    def __init__(self) -> None:
        import time  # noqa: PLC0415

        self._now = time.perf_counter
        self._last = self._now()
        self.parts: dict[str, float] = {}
        _ACTIVE.set(self)  # the step's clock until the next step makes its own

    def mark(self, name: str) -> None:
        now = self._now()
        if name:
            self.parts[name] = self.parts.get(name, 0.0) + (now - self._last) * 1000
        self._last = now

    def report(self, session: str, step: int, calls: int) -> None:
        from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

        ms = {f"{k}_ms": round(v, 2) for k, v in self.parts.items()}
        stream_audit("react.step.overhead", session_id=session, step=step, calls=calls, **ms)
