"""No-progress watchdog vs. honest commitment waits (iowarp/clio-agent#1230).

The turn-level no-progress watchdog (``await_turn_work``) must not kill a turn
whose single tool call is a declared, unbounded ``wait_for_terminal``
commitment (#1225) — that is an honest wait, not a stall. It must still fire,
typed, for a turn genuinely burning wall-clock with no progress signal at all.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from clio_agent.gact.runtime.globals import _TurnTimedOut
from clio_agent.gact.turn_watchdog import await_turn_work
from clio_agent.runtime import commitment_activity


def setup_function() -> None:
    commitment_activity._INFLIGHT.clear()


def teardown_function() -> None:
    commitment_activity._INFLIGHT.clear()


class _FakeBus:
    """A bus that never publishes -- isolates the commitment-wait signal."""

    def last_publish_monotonic(self, sid: str) -> float:
        return 0.0


def _state(*, timeout_s: float, poll_s: float, sid: str = "sess-1") -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(bus=_FakeBus())),
        sid=sid,
        bus=_FakeBus(),
        turn_cancel_event=threading.Event(),
        turn_progress_timeout_s=timeout_s,
        _watchdog_poll_s=poll_s,
    )


async def _commitment_wait(sid: str, hold_s: float) -> str:
    """Simulates tools/execution.py's commitment-tracked unbounded wait."""
    commitment_activity._INFLIGHT[sid] = 1
    try:
        await asyncio.sleep(hold_s)
    finally:
        commitment_activity._INFLIGHT.pop(sid, None)
    return "DONE"


@pytest.mark.asyncio
async def test_commitment_wait_pauses_the_ceiling_and_completes() -> None:
    """A turn whose single tool call is a commitment wait LONGER than the
    ceiling must complete instead of dying at the wall."""

    state = _state(timeout_s=0.15, poll_s=0.03)
    result = await await_turn_work(state, _commitment_wait(state.sid, 0.45))
    assert result == "DONE"
    assert not state.turn_cancel_event.is_set()


@pytest.mark.asyncio
async def test_wall_clock_outside_a_commitment_still_hits_the_ceiling() -> None:
    """No commitment in flight, no bus progress, no LM activity: the ceiling
    remains a real runaway backstop."""

    state = _state(timeout_s=0.1, poll_s=0.02)

    async def _silent_stall() -> str:
        await asyncio.sleep(1.0)
        return "SHOULD_NOT_REACH"

    with pytest.raises(_TurnTimedOut):
        await await_turn_work(state, _silent_stall())
    assert state.turn_cancel_event.is_set()


@pytest.mark.asyncio
async def test_commitment_wait_is_session_scoped() -> None:
    """A commitment wait open for a DIFFERENT session must not pause THIS
    session's watchdog (#761 defect-2 shape)."""

    state = _state(timeout_s=0.1, poll_s=0.02, sid="sess-victim")

    async def _neighbor_then_stall() -> str:
        commitment_activity._INFLIGHT["sess-other"] = 1
        try:
            await asyncio.sleep(1.0)
        finally:
            commitment_activity._INFLIGHT.pop("sess-other", None)
        return "SHOULD_NOT_REACH"

    with pytest.raises(_TurnTimedOut):
        await await_turn_work(state, _neighbor_then_stall())
