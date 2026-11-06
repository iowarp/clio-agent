"""The Core I/O thread runs clio-core calls in place, and batch failures are never lost.

``CoreIO``'s one thread lives until close, so it is declared native: a clio-core call on
it is not handed to a native-pool worker. A batch's future has no waiter, so an
exception its runner lets escape is logged typed. A read-only record group hands each
outcome back to its caller's loop on its own: one closed loop does not leave the rest
of the group unsettled.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from types import SimpleNamespace

import pytest

from clio_agent.arc.native_threads import run_on_native_thread
from clio_agent.gact.core_io import CORE_IO_BATCH_FAILED, CoreIO
from clio_agent.gact.read_only_outcomes import (
    READ_ONLY_OUTCOME_UNSETTLED,
    ReadOnlyOutcomes,
    _Outcome,
)


@pytest.mark.asyncio
async def test_the_core_io_thread_runs_native_calls_in_place() -> None:
    """SABOTAGE: drop ``mark_native_thread()`` from ``core_io._remember_thread`` and the
    call runs on an ``arc-native`` worker instead of the Core I/O thread."""
    io = CoreIO()
    try:
        here, native = await io.run(
            lambda: (threading.current_thread(), run_on_native_thread(threading.current_thread))
        )
    finally:
        await io.close()
    assert here.name.startswith("clio-core-io")
    assert native is here


@pytest.mark.asyncio
async def test_a_failed_batch_is_logged_typed(caplog: pytest.LogCaptureFixture) -> None:
    """SABOTAGE: drop the done-callback in ``CoreIO.submit_batch`` and the runner's
    exception vanishes with the batch future."""

    def runner(_items: list[str]) -> None:
        raise RuntimeError("group not confirmed")

    io = CoreIO()
    try:
        with caplog.at_level(logging.ERROR, logger="clio_agent.gact.core_io"):
            io.submit_batch(runner, "a")
            await io.run(lambda: None)  # FIFO: the batch ran (and its callback fired) first
    finally:
        await io.close()
    assert CORE_IO_BATCH_FAILED in caplog.text
    assert "RuntimeError" in caplog.text


def _item(identity: str, loop: asyncio.AbstractEventLoop, done: asyncio.Future[None]) -> _Outcome:
    return _Outcome(identity, lambda: None, (), None, (), contextvars.copy_context(), loop, done)


@pytest.mark.asyncio
async def test_a_closed_callers_loop_does_not_leave_the_rest_of_the_group_unsettled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SABOTAGE: drop the per-item ``try`` in ``ReadOnlyOutcomes._run`` and the closed
    loop's ``RuntimeError`` ends the hand-back: the second record's waiter never settles."""
    gone = asyncio.new_event_loop()
    gone_done = gone.create_future()
    gone.close()
    loop = asyncio.get_running_loop()
    live_done: asyncio.Future[None] = loop.create_future()
    history = SimpleNamespace(run_grouped=lambda operations: [(True, None)] * len(operations))
    outcomes = ReadOnlyOutcomes(history, recovery=None)  # type: ignore[arg-type]
    items = [_item("gone", gone, gone_done), _item("live", loop, live_done)]
    with caplog.at_level(logging.ERROR, logger="clio_agent.gact.read_only_outcomes"):
        await asyncio.to_thread(outcomes._run, items)
        await asyncio.wait_for(live_done, 5)
    assert READ_ONLY_OUTCOME_UNSETTLED in caplog.text
    assert "identity=gone" in caplog.text
