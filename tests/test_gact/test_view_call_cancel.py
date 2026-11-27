"""A view's call outside a turn is bound to its request, so it can be cancelled.

There is no turn, so the session's Cancel never reached such a call, and a call that
waited forever left the person at "Reopening" with nothing to press. The call is now
cancelled when its request goes away or CLIO shuts down, with a typed reason, and a
plain RPC no task holds is cancelled on the wire. No timer is involved.
"""

from __future__ import annotations

import asyncio
import contextvars
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
async def test_an_abandoned_plain_call_is_cancelled_on_the_wire_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It ends terminal without an attention episode (MC-F2): no agent is woken."""
    owner, recorded = _owner()
    owner.history = object()
    owner._attention = lambda *_args, **_kwargs: pytest.fail("a view's call woke the agent")
    settled: list[tuple[Any, str, str]] = []
    monkeypatch.setattr(
        connections.view_call_ledger,
        "settle_cut_off",
        lambda history, handle, text: settled.append((history, handle, text)),
    )
    rpc = asyncio.ensure_future(asyncio.sleep(60))
    event = threading.Event()
    event.set()
    with view_call_cancel.bound(event):
        await connections.abandon_call(owner, _host(), "task_a", rpc, None)
    await asyncio.sleep(0)
    assert rpc.cancelled()
    assert [identity for identity, _ in recorded] == ["external-call:task_a"]
    recorded[0][1]()
    assert settled == [(owner.history, "task_a", view_call_cancel.VIEW_CALL_CANCELLED_TEXT)]
    assert settled[0][2].startswith(view_call_cancel.VIEW_CALL_CANCELLED)


@pytest.mark.asyncio
async def test_a_turns_call_and_an_accepted_task_keep_their_custody() -> None:
    owner, recorded = _owner()
    turn_rpc = asyncio.ensure_future(asyncio.sleep(60))
    await connections.abandon_call(owner, _host(), "task_b", turn_rpc, None)
    task_rpc = asyncio.ensure_future(asyncio.sleep(60))
    event = threading.Event()
    event.set()
    with view_call_cancel.bound(event):
        await connections.abandon_call(owner, _host("task_c"), "task_c", task_rpc, None)
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
        await connections.abandon_call(owner, _host(), "task_d", rpc, object())
    await asyncio.sleep(0)
    assert rpc.cancelled()
    assert recorded == [] and owner._lazy == {}


def _raises_when_cancelled() -> Any:
    def invoke() -> str:
        from clio_agent.errors import CancellationError

        checker = make_cancellation_checker(app_holder[0])
        while not checker():
            threading.Event().wait(0.01)
        raise CancellationError("tool call cancelled by client")

    return invoke


@pytest.mark.asyncio
async def test_a_call_cancelled_by_its_gone_request_is_answered_typed() -> None:
    from fastapi import HTTPException

    app = _app()
    app_holder[:] = [app]
    request = _Request()
    work = asyncio.ensure_future(
        view_call_cancel.run(app, _raises_when_cancelled(), request=request)
    )
    await asyncio.sleep(0.05)
    request.gone.set()
    with pytest.raises(HTTPException) as raised:
        await asyncio.wait_for(work, 5)
    assert raised.value.status_code == 409
    assert str(raised.value.detail).startswith(view_call_cancel.VIEW_CALL_CANCELLED)


@pytest.mark.asyncio
async def test_a_call_cancelled_at_shutdown_without_a_request_still_raises() -> None:
    from clio_agent.errors import CancellationError

    app = _app()
    app_holder[:] = [app]
    work = asyncio.ensure_future(view_call_cancel.run(app, _raises_when_cancelled()))
    await asyncio.sleep(0.05)
    view_call_cancel.cancel_all(app)
    with pytest.raises(CancellationError):
        await asyncio.wait_for(work, 5)


def _stopped_app(sid: str, *, flag: bool = False, shutdown: bool = False) -> Any:
    event = threading.Event()
    if not flag:
        event.set()
    return SimpleNamespace(
        state=SimpleNamespace(
            cancel_events={sid: event},
            cancel_flags={sid} if flag else set(),
            turn_runner=SimpleNamespace(closing=shutdown, stop_requested=lambda _sid: False),
            uvicorn_server=None,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_a_stopped_turns_plain_call_is_cancelled_on_the_wire(
    monkeypatch: pytest.MonkeyPatch, flag: bool
) -> None:
    """Stop cancels the turn's plain call: typed record, unknown outcome, no wake."""
    owner, recorded = _owner()
    owner.history = object()
    owner.app = _stopped_app("sess_a", flag=flag)
    owner._attention = lambda *_args, **_kwargs: pytest.fail("a stopped call woke the agent")
    settled: list[tuple[Any, str, str]] = []
    monkeypatch.setattr(
        connections.view_call_ledger,
        "settle_cut_off",
        lambda history, handle, text: settled.append((history, handle, text)),
    )
    rpc = asyncio.ensure_future(asyncio.sleep(60))
    await connections.abandon_call(owner, _host(), "task_t", rpc, None, "sess_a")
    await asyncio.sleep(0)
    assert rpc.cancelled()
    recorded[0][1]()
    assert settled == [(owner.history, "task_t", connections.TURN_CALL_CANCELLED_TEXT)]
    assert settled[0][2].startswith(connections.TURN_CALL_CANCELLED + ":")


@pytest.mark.asyncio
async def test_a_stopped_scripts_plain_call_is_cancelled_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background script outlives its turn: its stop, not the turn's, cuts the call off.
    SABOTAGE: drop the ``scope_stopped()`` branch from ``_cut_off_text`` -> the call
    keeps waiting for a reply that a deleted session never takes -> red."""
    from clio_agent.runtime.cancellation import bind_scope_stop

    owner, recorded = _owner()
    owner.history = object()
    owner.app = _stopped_app("sess_other")  # the script's own turn is not stopped
    settled: list[tuple[Any, str, str]] = []
    monkeypatch.setattr(
        connections.view_call_ledger,
        "settle_cut_off",
        lambda history, handle, text: settled.append((history, handle, text)),
    )
    stop = threading.Event()
    stop.set()

    async def abandon(rpc: Any) -> None:
        bind_scope_stop(stop)
        await connections.abandon_call(owner, _host(), "task_x", rpc, None, "sess_a")

    rpc = asyncio.ensure_future(asyncio.sleep(60))
    await asyncio.create_task(abandon(rpc), context=contextvars.copy_context())
    await asyncio.sleep(0)
    assert rpc.cancelled()
    recorded[0][1]()
    assert settled == [(owner.history, "task_x", connections.SCRIPT_CALL_CANCELLED_TEXT)]
    assert settled[0][2].startswith(connections.TURN_CALL_CANCELLED + ":")


@pytest.mark.asyncio
async def test_stop_leaves_other_sessions_shutdown_and_accepted_tasks_alone() -> None:
    owner, recorded = _owner()
    owner.app = _stopped_app("sess_a")
    other = asyncio.ensure_future(asyncio.sleep(60))
    await connections.abandon_call(owner, _host(), "task_o", other, None, "sess_b")
    accepted = asyncio.ensure_future(asyncio.sleep(60))
    await connections.abandon_call(owner, _host("task_k"), "task_k", accepted, None, "sess_a")
    owner.app = _stopped_app("sess_a", shutdown=True)
    draining = asyncio.ensure_future(asyncio.sleep(60))
    await connections.abandon_call(owner, _host(), "task_s", draining, None, "sess_a")
    assert not (other.cancelled() or accepted.cancelled() or draining.cancelled())
    assert recorded == []
    for rpc in (other, accepted, draining):
        rpc.cancel()


def test_a_stopped_call_settles_its_parents_cancellation() -> None:
    """A cut-off plain call is terminal with ``settlement: unknown`` by design; the
    parent's cancellation must still count it settled or deleting the conversation
    waits on it forever (409). SABOTAGE: drop the cut-off clause -> red."""
    from clio_agent.gact.conversation_controls import _member_settled

    def call(**changes: Any) -> Any:
        fields = {
            "kind": "MCP",
            "terminal_at": "2026-01-01T00:00:00Z",
            "settlement": "unknown",
            "raw_status": connections.TURN_CALL_CANCELLED,
            "status": "cancelled",
            "result": None,
        }
        return SimpleNamespace(**(fields | changes))

    assert _member_settled(call())
    assert not _member_settled(call(terminal_at=None))  # still running
    assert not _member_settled(call(raw_status="mcp_call_interrupted"))  # restart: unknown
    assert not _member_settled(call(kind="Code"))  # local work confirms its own retirement
