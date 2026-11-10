"""Fail-closed clearance over real Core watcher state and common mailbox progress."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from clio_agent.gact.native_work import now
from clio_agent.gact.spotter_clearance import (
    CLEARANCE_CANCELLED,
    CLEARANCE_GRANTED,
    CLEARANCE_PROGRESS_STALLED,
    CLEARANCE_WATCHER_FAILED,
    CLEARANCE_WATCHER_UNAVAILABLE,
    SPOTTER_CLEARANCE_REASONS,
    clearance_event,
    release_clearance_event,
    signal_clearance,
    wait_for_spotter_clearance,
)
from clio_agent.gact.spotter_watcher import disarm_spotter_watcher, wake_on_parent_activity


def _join_admission(app: Any, sid: str) -> None:
    """Join ``sid``'s in-flight admission checks on the owner loop, where they run."""

    async def join() -> None:
        runner = app.state.turn_runner
        while (pump := runner._pumps.get(sid)) is not None and not pump.done():
            await asyncio.shield(pump)
        assert not runner.busy(sid)

    asyncio.run_coroutine_threadsafe(join(), app.state.conversations._loop).result(timeout=5)


def test_progress_after_timed_out_poll_is_not_discarded(
    watcher_world: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal arriving after wait returns false still refreshes the real progress window."""
    app, parent, child = watcher_world
    owner = app.state.conversations

    # Pause both model owners and join their actual control work. The child's
    # attributed pause notice must not start a parent turn that publishes more
    # watcher evidence while this test asserts the two controlled progress polls.
    async def join_admission() -> None:
        # Control notices can reserve a final admission-only probe. Join those
        # checks and inspect retirement on the owner loop; sampling busy from
        # the test thread races with a correctly vetoed, worker-free probe.
        for sid in (parent, child):
            while True:
                pump = app.state.turn_runner._pumps.get(sid)
                if pump is not None and not pump.done():
                    await asyncio.shield(pump)
                    continue
                assert not app.state.turn_runner.busy(sid)
                assert owner.model_paused(sid)
                break

    async def hold_models() -> None:
        for sid in (parent, child):
            assignment = app.state.task_history.latest_assignment(sid)
            await owner.pause(owner.caller(sid), assignment.handle)
        await asyncio.gather(*tuple(owner._controls.values()))
        await join_admission()

    asyncio.run_coroutine_threadsafe(hold_models(), owner._loop).result(timeout=5)
    wake_on_parent_activity(app, parent, tool_name="fs_read_file")
    event = clearance_event(app, parent)
    original_wait = event.wait
    polls: list[bool] = []

    def signal_after_poll(timeout: float | None = None) -> bool:
        if not polls:
            result = original_wait(timeout)
            assert not result
            polls.append(result)
            signal_clearance(app, parent)
            return result
        for item in app.state.task_history.pending_mail(child):
            app.state.task_history.withdraw_mail(child, item.id)
        # Confirmed withdrawal also wakes admission. Join that worker-free
        # probe before asserting the next clearance state, on the owner loop.
        asyncio.run_coroutine_threadsafe(join_admission(), owner._loop).result(timeout=5)
        signal_clearance(app, parent)
        polls.append(True)
        return original_wait(timeout)

    monkeypatch.setattr(event, "wait", signal_after_poll)
    assert wait_for_spotter_clearance(app, parent, progress_timeout_s=0.05) == CLEARANCE_GRANTED
    assert polls == [False, True]


def test_idle_healthy_watcher_clears(watcher_world: tuple[Any, str, str]) -> None:
    app, parent, _child = watcher_world
    assert wait_for_spotter_clearance(app, parent, progress_timeout_s=0) == CLEARANCE_GRANTED


def test_stop_releases_clearance_wait_without_cancelling_the_watcher(
    watcher_world: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invocation cancellation keeps independent watcher evidence and execution intact."""
    app, parent, child = watcher_world
    wake_on_parent_activity(app, parent, tool_name="fs_read_file")
    before = app.state.task_history.latest_assignment(child)
    pending = app.state.task_history.pending_mail(child)
    stopped, entered = threading.Event(), threading.Event()
    app.state.cancel_events = {parent: stopped}
    event = clearance_event(app, parent)
    original_wait = event.wait

    def entered_wait(timeout: float | None = None) -> bool:
        entered.set()
        return original_wait(timeout)

    monkeypatch.setattr(event, "wait", entered_wait)
    outcomes: list[str] = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            wait_for_spotter_clearance(app, parent, progress_timeout_s=30)
        )
    )
    worker.start()
    try:
        assert entered.wait(2), "The real clearance wait never started"
        stopped.set()
        assert app.state.cancel_events.pop(parent) is stopped
        worker.join(timeout=2)
        assert not worker.is_alive(), "Stop left the invocation awaiting watcher progress"
        assert outcomes == ["spotter_clearance_cancelled"]
        assert app.state.task_history.latest_assignment(child) == before
        assert app.state.task_history.pending_mail(child) == pending
    finally:
        if worker.is_alive():
            disarm_spotter_watcher(app, app.state.sessions.get(parent))
            signal_clearance(app, parent)
            worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("status", ["failed", "interrupted", "task_state_uncertain"])
def test_failed_or_uncertain_watcher_fails_closed(
    watcher_world: tuple[Any, str, str], status: str
) -> None:
    app, parent, child = watcher_world
    task = app.state.task_history.latest_assignment(child)
    changed = task.model_copy(
        update={
            "status": status,
            "raw_status": status,
            "terminal_at": now() if status != "task_state_uncertain" else None,
        }
    )
    app.state.task_history.record_snapshot(changed, observation_id="watcher-failure:" + status)
    assert wait_for_spotter_clearance(app, parent, progress_timeout_s=0) == CLEARANCE_WATCHER_FAILED


def test_armed_session_without_live_watcher_fails_closed(
    watcher_world: tuple[Any, str, str],
) -> None:
    app, parent, _child = watcher_world
    disarm_spotter_watcher(app, app.state.sessions.get(parent))
    assert (
        wait_for_spotter_clearance(app, parent, progress_timeout_s=0)
        == CLEARANCE_WATCHER_UNAVAILABLE
    )


def test_non_spotter_session_needs_no_watcher(watcher_world: tuple[Any, str, str]) -> None:
    app, parent, _child = watcher_world
    app.state.sessions.update(parent, approval_mode="ask")
    assert wait_for_spotter_clearance(app, parent, progress_timeout_s=0) == CLEARANCE_GRANTED


def test_uncommitted_coalesced_evidence_holds_clearance(
    watcher_world: tuple[Any, str, str],
) -> None:
    app, parent, child = watcher_world
    wake_on_parent_activity(app, parent, tool_name="fs_read_file")
    # The first wake's mail wakes the watcher's admission, which reserves the watcher
    # while it checks; a wake sampled in that window is rightly queued as a new mail
    # (the watcher may be reading the first). Join that worker-free probe first.
    _join_admission(app, child)
    wake_on_parent_activity(app, parent, tool_name="fs_read_file")
    assert len(app.state.task_history.pending_mail(child)) == 1
    assert (
        wait_for_spotter_clearance(app, parent, progress_timeout_s=0) == CLEARANCE_PROGRESS_STALLED
    )


def test_every_progress_signal_restarts_only_the_progress_window(
    watcher_world: tuple[Any, str, str],
) -> None:
    app, parent, child = watcher_world
    wake_on_parent_activity(app, parent, tool_name="fs_read_file")
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(
            wait_for_spotter_clearance(app, parent, progress_timeout_s=0.2)
        )
    )
    worker.start()
    try:
        for _ in range(3):
            time.sleep(0.08)
            signal_clearance(app, parent)
            assert worker.is_alive()
        for item in app.state.task_history.pending_mail(child):
            app.state.task_history.withdraw_mail(child, item.id)
        signal_clearance(app, parent)
        worker.join(timeout=5)
        assert not worker.is_alive() and result == [CLEARANCE_GRANTED]
    finally:
        if worker.is_alive():
            disarm_spotter_watcher(app, app.state.sessions.get(parent))
            signal_clearance(app, parent)
            worker.join(timeout=5)
        assert not worker.is_alive()


def test_release_sets_waiter_event_and_removes_its_hint(
    watcher_world: tuple[Any, str, str],
) -> None:
    app, parent, _child = watcher_world
    event = clearance_event(app, parent)
    event.clear()
    assert release_clearance_event(app, parent)
    assert event.is_set() and parent not in app.state.spotter_clearance_events


def test_every_denial_has_a_distinct_model_facing_explanation() -> None:
    reasons = {
        CLEARANCE_WATCHER_UNAVAILABLE,
        CLEARANCE_WATCHER_FAILED,
        CLEARANCE_PROGRESS_STALLED,
        CLEARANCE_CANCELLED,
    }
    assert reasons == set(SPOTTER_CLEARANCE_REASONS)
    assert all(SPOTTER_CLEARANCE_REASONS[reason].strip() for reason in reasons)
    assert len({SPOTTER_CLEARANCE_REASONS[reason] for reason in reasons}) == len(reasons)
