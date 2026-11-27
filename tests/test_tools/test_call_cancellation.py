"""A tool call's synchronous waits end when the person stops its turn."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import pytest

from clio_agent.errors import CancellationError
from clio_agent.runtime.cancellation import cancellation_checker
from clio_agent.runtime.fresh_loop import run_in_new_loop
from clio_agent.tools import call_cancellation


def test_a_stopped_wait_on_shared_work_leaves_it_running() -> None:
    work: concurrent.futures.Future[str] = concurrent.futures.Future()
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()

    started = time.monotonic()
    with cancellation_checker(stop.is_set), pytest.raises(CancellationError) as raised:
        call_cancellation.wait_future(work, stage="join listing spot")

    assert time.monotonic() - started < 2
    assert raised.value.details["left_behind"] is True
    assert not work.cancelled(), "shared work goes on for whoever needs it next"


def test_a_stopped_call_stops_the_work_it_owns() -> None:
    stop = threading.Event()
    closed = threading.Event()

    async def listing() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()

    threading.Timer(0.2, stop.set).start()
    with cancellation_checker(stop.is_set), pytest.raises(CancellationError):
        run_in_new_loop(call_cancellation.until_cancelled(listing(), stage="list spot"))

    assert closed.is_set(), "the call's own listing closed before the call returned"


def test_an_unstopped_wait_returns_the_result_and_keeps_its_timeout() -> None:
    work: concurrent.futures.Future[str] = concurrent.futures.Future()
    threading.Timer(0.1, work.set_result, args=("listed",)).start()
    assert call_cancellation.wait_future(work, stage="x") == "listed"

    never: concurrent.futures.Future[str] = concurrent.futures.Future()
    with pytest.raises(concurrent.futures.TimeoutError):
        call_cancellation.wait_future(never, 0.2, stage="x")
    assert not call_cancellation.wait_event(threading.Event(), 0.2, stage="x")
