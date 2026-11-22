"""A finished script's close waits for the calls it sent, unless its task is cancelled.

A script may end with a nested call still running that it never awaited (a call is made
when the script makes it). Its close waits for that call; but a Cancel while it waits
must settle the task, not wait for the call to end.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Any

import pytest

from clio_agent.gact.code_mode.dispatch import Dispatcher

pytestmark = pytest.mark.timeout(30)


def _dispatcher(release: threading.Event) -> Dispatcher:
    def slow(_args: Any) -> str:
        release.wait(20)
        return "done"

    return Dispatcher(
        {}, (), contextvars.copy_context(), lambda _q, _n: [], reserved={"__slow__": slow}
    )


def test_a_cancel_while_a_finished_script_waits_for_its_calls_ends_the_wait() -> None:
    """SABOTAGE: ``pool.shutdown(wait=True)`` for a finished script (the old close) ->
    close waits the call's 20 s past the cancel -> red."""
    release = threading.Event()
    dispatcher = _dispatcher(release)
    replies: list[dict[str, Any]] = []
    dispatcher.submit({"id": 1, "tool": "__slow__", "args": {}}, replies.append)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    try:
        dispatcher.close(finished=True, cancelled=cancel.is_set, poll_s=0.05)
        assert time.monotonic() - started < 5
    finally:
        release.set()


def test_a_finished_script_still_waits_for_a_call_it_did_not_await() -> None:
    release = threading.Event()
    dispatcher = _dispatcher(release)
    replies: list[dict[str, Any]] = []
    dispatcher.submit({"id": 1, "tool": "__slow__", "args": {}}, replies.append)
    threading.Timer(0.2, release.set).start()
    dispatcher.close(finished=True, cancelled=lambda: False, poll_s=0.05)
    assert replies and replies[0]["value"] == "done"


def test_stopping_a_script_cancels_its_calls_in_flight() -> None:
    """A stopped script's running call sees the stop through the executor's checker
    (``with_scope_stop``), as an MCP call polling for cancellation does, and ends with
    a cancellation. SABOTAGE: drop ``self.stopped.set()`` from ``close`` -> the call holds
    until its 20 s release -> red."""
    from clio_agent.errors import CancellationError
    from clio_agent.runtime.cancellation import with_scope_stop

    release = threading.Event()
    running = threading.Event()

    def hold() -> str:
        checker = with_scope_stop(None)  # what ``_call_tool_inner`` polls
        running.set()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not release.is_set():
            if checker is not None and checker():
                raise CancellationError("tool call cancelled by client")
            time.sleep(0.02)
        return "held"

    dispatcher = Dispatcher({"probe_hold": hold}, (), contextvars.copy_context(), lambda *_: [])
    replies: list[dict[str, Any]] = []
    ended = threading.Event()
    dispatcher.submit(
        {"id": 1, "tool": "probe_hold", "args": {}}, lambda r: (replies.append(r), ended.set())
    )
    try:
        assert running.wait(5)
        started = time.monotonic()
        dispatcher.close(finished=False)
        assert ended.wait(5), "the call in flight kept running after the script stopped"
        assert time.monotonic() - started < 5
        assert replies[0]["ok"] is False and "CancellationError" in replies[0]["error"]
    finally:
        release.set()


def test_a_call_outside_a_script_keeps_its_own_checker() -> None:
    """No script scope: the executor's checker is the turn's own, unchanged."""
    from clio_agent.runtime.cancellation import with_scope_stop

    def base() -> bool:
        return False

    assert with_scope_stop(base) is base
    assert with_scope_stop(None) is None
