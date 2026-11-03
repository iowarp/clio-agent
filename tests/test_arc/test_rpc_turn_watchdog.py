"""Core RPC liveness owns storage waits; provider silence remains session scoped."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import rpc_liveness
from clio_agent.gact import context, turn_watchdog
from clio_agent.gact.runtime.globals import _TurnTimedOut
from clio_agent.gact.turn_watchdog import await_turn_work
from clio_agent.runtime import commitment_activity
from clio_agent.runtime.commitment_activity import commitment_wait_in_flight


class QuietBus:
    """Supply no unrelated progress to the real provider watchdog."""

    def last_publish_monotonic(self, session_id: str) -> float:
        return 0.0


async def _entered_or_ended(entered: threading.Event, work: asyncio.Task[Any]) -> None:
    """Wait until the storage read is inside its native wait, with no wall-clock budget.

    Starting a worker thread on a loaded runner can take over a second. A read that
    ends without entering the wait fails here at once; a real hang is the per-test hang
    guard's (tests/_hang_guard.py).
    """
    while not entered.is_set():
        assert not work.done(), "the storage read ended before entering its native wait"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_session", [True, False])
async def test_storage_wait_is_scoped_and_ends_before_provider_silence(
    monkeypatch: pytest.MonkeyPatch, same_session: bool
) -> None:
    """Slow storage cannot look like a stalled model or excuse another stalled session."""
    entered = threading.Event()
    release = threading.Event()
    owner = "rpc-owner"

    def native_wait(operation: Any, _stall_after_s: float) -> tuple[str, Any, None]:
        assert commitment_wait_in_flight(owner)
        entered.set()
        assert release.wait(3), "test did not release its storage owner"
        return "done", operation(), None

    monkeypatch.setattr(rpc_liveness, "_run_with_stall_watch", native_wait)
    state = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(bus=QuietBus())),
        sid=owner if same_session else "unrelated-model",
        turn_cancel_event=threading.Event(),
        turn_progress_timeout_s=0.1,
        _watchdog_poll_s=0.02,
    )

    def storage_read() -> str:
        token = context.set_session_id(owner)
        try:
            return rpc_liveness.call_with_liveness(
                lambda: "confirmed",
                op_name="get",
                port=9413,
                reconnect=lambda: pytest.fail("healthy read must not reconnect"),
            )
        finally:
            context.reset(token)

    storage = asyncio.create_task(asyncio.to_thread(storage_read))
    try:
        await _entered_or_ended(entered, storage)
        watched = asyncio.create_task(await_turn_work(state, asyncio.shield(storage)))
        if same_session:
            await asyncio.sleep(0.25)
            assert not watched.done() and not state.turn_cancel_event.is_set()
            release.set()
            assert await watched == "confirmed"
            assert not commitment_wait_in_flight(owner)

            async def silent_model() -> None:
                await asyncio.sleep(1)

            with pytest.raises(_TurnTimedOut):
                await await_turn_work(state, silent_model())
        else:
            with pytest.raises(_TurnTimedOut):
                await watched
            assert commitment_wait_in_flight(owner)
    finally:
        release.set()
        assert await storage == "confirmed"
    assert not commitment_wait_in_flight(owner)


def test_core_failure_releases_wait_activity_and_preserves_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage failure is not reclassified as a provider timeout or a successful read."""
    failure = OSError("native storage read failed")

    def failed(operation: Any, _stall_after_s: float) -> tuple[str, None, OSError]:
        assert commitment_wait_in_flight("failed-rpc-owner")
        return "done", None, failure

    monkeypatch.setattr(rpc_liveness, "_run_with_stall_watch", failed)
    token = context.set_session_id("failed-rpc-owner")
    try:
        with pytest.raises(OSError) as caught:
            rpc_liveness.call_with_liveness(
                lambda: None, op_name="get", port=9413, reconnect=lambda: None
            )
        assert caught.value is failure
    finally:
        context.reset(token)
    assert not commitment_wait_in_flight("failed-rpc-owner")


@pytest.mark.asyncio
async def test_watchdog_observes_a_storage_wait_that_ended_between_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delayed guard poll uses actual wait completion, not just current in-flight state."""
    clock = [0.0]
    controlled_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(turn_watchdog, "time", controlled_time)
    monkeypatch.setattr(commitment_activity, "time", controlled_time)

    def native_reply(operation: Any, _stall_after_s: float) -> tuple[str, Any, None]:
        clock[0] = 0.3
        return "done", operation(), None

    monkeypatch.setattr(rpc_liveness, "_run_with_stall_watch", native_reply)
    state = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(bus=QuietBus())),
        sid="between-polls",
        turn_cancel_event=threading.Event(),
        turn_progress_timeout_s=0.2,
        _watchdog_poll_s=0.2,
    )
    polls = 0

    async def poll(tasks: Any, *, timeout: float) -> tuple[set[Any], set[Any]]:
        nonlocal polls
        polls += 1
        assert timeout == 0.2
        if polls == 1:
            token = context.set_session_id(state.sid)
            try:
                assert (
                    rpc_liveness.call_with_liveness(
                        lambda: "reply", op_name="get", port=9413, reconnect=lambda: None
                    )
                    == "reply"
                )
            finally:
                context.reset(token)
            assert not commitment_wait_in_flight(state.sid)
            clock[0] = 0.31
            return set(), tasks
        [task] = tasks
        await task
        return tasks, set()

    monkeypatch.setattr(turn_watchdog.asyncio, "wait", poll)

    async def model() -> str:
        return "continued"

    assert await await_turn_work(state, model()) == "continued"
    assert polls == 2 and not state.turn_cancel_event.is_set()
