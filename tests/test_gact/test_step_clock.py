"""The step clock names CLIO's own time in a step and leaves the model and tools out."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.agents import step_clock
from clio_agent.runtime import stream_audit


def test_marks_add_up_by_part_and_an_empty_mark_only_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: count the time before an empty mark and ``build`` reads 7 s."""
    ticks = iter([0.0, 0.010, 1.0, 1.002, 8.0, 8.030, 9.0, 9.001])
    monkeypatch.setattr(step_clock.StepClock, "__init__", _init(ticks))
    rows: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(stream_audit, "stream_audit", lambda stage, **f: rows.append((stage, f)))
    clock = step_clock.StepClock()
    clock.mark("prep")  # 10 ms
    clock.mark("")  # the wait until the request is built: not CLIO's step work
    clock.mark("flush")  # 2 ms
    clock.mark("")  # model call and tools
    clock.mark("record")  # 30 ms
    clock.mark("")
    clock.mark("record")  # 1 ms more
    clock.report("sess_1", 3, 2)
    assert rows == [
        (
            "react.step.overhead",
            {"session_id": "sess_1", "step": 3, "calls": 2,
             "prep_ms": 10.0, "flush_ms": 2.0, "record_ms": 31.0},
        )
    ]  # fmt: skip


def _init(ticks: Any) -> Any:
    def init(self: step_clock.StepClock) -> None:
        self._now = lambda: next(ticks)
        self._last = self._now()
        self.parts = {}

    return init


def test_code_the_step_calls_marks_the_steps_own_clock() -> None:
    """``record`` was one figure (44-66 ms a step on L1P); the step's writes now mark
    their own parts on the running step's clock. SABOTAGE: drop the clock's
    registration and ``record_write`` never appears."""
    import contextvars

    def step() -> dict[str, float]:
        step_clock.mark("record_write")  # no step yet: nothing happens
        clock = step_clock.StepClock()
        step_clock.mark("")
        step_clock.mark("record_write")
        return clock.parts

    parts = contextvars.copy_context().run(step)
    assert set(parts) == {"record_write"}
