"""Hand-offs onto the app loop fail typed, never as a bare TimeoutError (#1577 3.9).

``spawn_child_turn_threadsafe`` and the spotter watcher's check-turn start used
``run_coroutine_threadsafe(...).result(timeout=60/30)``: a stopped loop made the caller
wait out the bound, then raise an untyped ``TimeoutError`` while the work could still run
later. :func:`call_on_loop` waits while the loop is alive and processing, fails at once
(typed) on a loop that is not running, fails typed on a loop stalled for the ceiling, and
never runs an abandoned call later. Driven on REAL event loops in threads.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator

import pytest

from clio_agent.gact import loop_handoff
from clio_agent.gact.loop_handoff import LoopHandoffError, call_on_loop


@pytest.fixture
def running_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def test_a_live_loop_runs_the_call_and_returns_its_value(
    running_loop: asyncio.AbstractEventLoop,
) -> None:
    on_loop: list[bool] = []

    def _work() -> int:
        on_loop.append(asyncio.get_running_loop() is running_loop)
        return 7

    assert call_on_loop(running_loop, _work, op="test") == 7
    assert on_loop == [True]


def test_a_loop_that_is_not_running_fails_typed_at_once() -> None:
    """SABOTAGE: restore ``.result(timeout=60)`` -> the call waits 60 s and raises a bare
    TimeoutError -> the hang guard / the elapsed assertion fails -> red."""
    loop = asyncio.new_event_loop()  # created, never run: nothing will ever process it
    try:
        started = time.monotonic()
        with pytest.raises(LoopHandoffError) as caught:
            call_on_loop(loop, lambda: None, op="spawn_child_turn")
        assert caught.value.reason == loop_handoff.REASON_LOOP_NOT_RUNNING
        assert "spawn_child_turn" in str(caught.value)
        assert time.monotonic() - started < 5
    finally:
        loop.close()


def test_a_busy_loop_is_waited_for(
    running_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop is blocked 1 s by other work; slices of 0.2 s and a 5 s ceiling: it runs.

    SABOTAGE: give up after one slice without an answer -> LoopHandoffError -> red.
    """
    monkeypatch.setattr(loop_handoff, "HANDOFF_SLICE_S", 0.2)
    monkeypatch.setattr(loop_handoff, "STALL_CEILING_S", 5.0)
    running_loop.call_soon_threadsafe(time.sleep, 1.0)
    assert call_on_loop(running_loop, lambda: "ran", op="test") == "ran"


def test_a_stalled_loop_fails_typed_and_never_runs_the_call_later(
    running_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop is wedged 3 s; ceiling 0.6 s: typed stall, and the call is abandoned.

    SABOTAGE: drop the abandon check in the loop-side wrapper -> the call runs once the
    loop frees -> ``ran`` is set -> red.
    """
    monkeypatch.setattr(loop_handoff, "HANDOFF_SLICE_S", 0.2)
    monkeypatch.setattr(loop_handoff, "STALL_CEILING_S", 0.6)
    ran = threading.Event()
    running_loop.call_soon_threadsafe(time.sleep, 3.0)
    with pytest.raises(LoopHandoffError) as caught:
        call_on_loop(running_loop, ran.set, op="spotter_check_turn")
    assert caught.value.reason == loop_handoff.REASON_LOOP_STALLED
    time.sleep(3.0)  # the loop frees; the abandoned call must not run now
    done = threading.Event()
    running_loop.call_soon_threadsafe(done.set)
    assert done.wait(5)
    assert not ran.is_set()


def test_a_call_that_raises_reraises_its_own_error(
    running_loop: asyncio.AbstractEventLoop,
) -> None:
    def _boom() -> None:
        raise ValueError("bad spec")

    with pytest.raises(ValueError, match="bad spec"):
        call_on_loop(running_loop, _boom, op="test")
