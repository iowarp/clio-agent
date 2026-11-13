"""A view's call outside a turn is bound to its request, so it can be cancelled.

There is no turn, so the session's Cancel never reached such a call, and a call that
waited forever left the person at "Reopening" with nothing to press. The call is now
cancelled when its request goes away or CLIO shuts down, with a typed reason, and a
plain RPC no task holds is cancelled on the wire. No timer is involved.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import external_task_connections as connections
from clio_agent.gact import view_call_cancel
from clio_agent.gact.cancellation_check import make_cancellation_checker


class _Request:
    """A request that reports a disconnect once ``gone`` is set."""

    def __init__(self) -> None:
        self.gone = threading.Event()

    async def is_disconnected(self) -> bool:
        return self.gone.is_set()


def _app() -> Any:
    sessions = SimpleNamespace(most_recent=lambda: None, get=lambda _sid: None)
    return SimpleNamespace(
        state=SimpleNamespace(cancel_events={}, cancel_flags=set(), sessions=sessions)
    )


def _waits_for_cancel(seen: list[bool]) -> Any:
    def invoke() -> str:
        # Stands in for _run_foreground_coroutine polling the session's checker.
        checker = make_cancellation_checker(app_holder[0])
        while not checker():
            threading.Event().wait(0.01)
        seen.append(view_call_cancel.requested())
        return "cancelled"

    return invoke


app_holder: list[Any] = []


@pytest.mark.asyncio
async def test_a_disconnected_request_cancels_its_call() -> None:
    app = _app()
    app_holder[:] = [app]
    request, seen = _Request(), []
    work = asyncio.ensure_future(
        view_call_cancel.run(app, _waits_for_cancel(seen), request=request)
    )
    await asyncio.sleep(0.05)
    assert not work.done()
    assert len(app.state.view_call_cancels) == 1
    request.gone.set()
    assert await asyncio.wait_for(work, 5) == "cancelled"
    assert seen == [True]
    assert app.state.view_call_cancels == set()


@pytest.mark.asyncio
async def test_shutdown_cancels_every_view_call_in_flight() -> None:
    app = _app()
    app_holder[:] = [app]
    seen: list[bool] = []
    first = asyncio.ensure_future(view_call_cancel.run(app, _waits_for_cancel(seen)))
    second = asyncio.ensure_future(view_call_cancel.run(app, _waits_for_cancel(seen)))
    await asyncio.sleep(0.05)
    assert view_call_cancel.cancel_all(app) == 2
    assert await asyncio.wait_for(asyncio.gather(first, second), 5) == ["cancelled"] * 2
    assert seen == [True, True]


@pytest.mark.asyncio
async def test_a_cancelled_caller_cancels_the_call_it_awaited() -> None:
    app = _app()
    app_holder[:] = [app]
    seen: list[bool] = []
    events: list[threading.Event] = []

    def invoke() -> str:
        events.extend(app.state.view_call_cancels)
        return _waits_for_cancel(seen)()

    work = asyncio.ensure_future(view_call_cancel.run(app, invoke))
    await asyncio.sleep(0.05)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert events and events[0].is_set()


def test_a_call_outside_any_view_is_untouched() -> None:
    app = _app()
    assert view_call_cancel.requested() is False
    assert view_call_cancel.bound_here() is False
    assert make_cancellation_checker(app)() is False


def _host(accepted: str = "") -> Any:
    calls = {}
    if accepted:
        calls["t1"] = SimpleNamespace(submission=SimpleNamespace(handle=accepted))
    return SimpleNamespace(calls=calls)


def _owner() -> Any:
    recorded: list[tuple[str, Any]] = []

    async def record_or_defer(identity: str, write: Any, *, retire: Any = None) -> None:
        recorded.append((identity, write))

    return SimpleNamespace(_record_or_defer=record_or_defer, _attention=None, _lazy={}), recorded


@pytest.mark.asyncio
async def test_an_abandoned_plain_call_is_cancelled_on_the_wire_and_recorded() -> None:
    owner, recorded = _owner()
    attention: list[tuple[str, str, bool]] = []
    owner._attention = lambda handle, text, lost=True: attention.append((handle, text, lost))
    rpc = asyncio.ensure_future(asyncio.sleep(60))
    event = threading.Event()
    event.set()
    with view_call_cancel.bound(event):
        await connections.abandon_view_call(owner, _host(), "task_a", rpc, None)
    await asyncio.sleep(0)
    assert rpc.cancelled()
    assert [identity for identity, _ in recorded] == ["external-call:task_a"]
    recorded[0][1]()
    assert attention == [("task_a", view_call_cancel.VIEW_CALL_CANCELLED_TEXT, False)]
    assert view_call_cancel.VIEW_CALL_CANCELLED in attention[0][1]


@pytest.mark.asyncio
async def test_a_turns_call_and_an_accepted_task_keep_their_custody() -> None:
    owner, recorded = _owner()
    turn_rpc = asyncio.ensure_future(asyncio.sleep(60))
    await connections.abandon_view_call(owner, _host(), "task_b", turn_rpc, None)
    task_rpc = asyncio.ensure_future(asyncio.sleep(60))
    event = threading.Event()
    event.set()
    with view_call_cancel.bound(event):
        await connections.abandon_view_call(owner, _host("task_c"), "task_c", task_rpc, None)
    assert not turn_rpc.cancelled() and not task_rpc.cancelled()
    assert recorded == []
    turn_rpc.cancel()
    task_rpc.cancel()


@pytest.mark.asyncio
async def test_an_abandoned_read_only_call_drops_its_unwritten_admission() -> None:
    owner, recorded = _owner()
    owner._lazy["task_d"] = object()
    rpc = asyncio.ensure_future(asyncio.sleep(60))
    event = threading.Event()
    event.set()
    with view_call_cancel.bound(event):
        await connections.abandon_view_call(owner, _host(), "task_d", rpc, object())
    await asyncio.sleep(0)
    assert rpc.cancelled()
    assert recorded == [] and owner._lazy == {}
