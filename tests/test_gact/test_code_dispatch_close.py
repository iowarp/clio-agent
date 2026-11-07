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
