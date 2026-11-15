"""Cancellation checks shared by the GACT tool-execution boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from clio_agent.gact import context as _ctx
from clio_agent.gact import view_call_cancel
from clio_agent.gact.runtime.globals import _resolve_tool_session

if TYPE_CHECKING:
    from fastapi import FastAPI


def make_cancellation_checker(app: "FastAPI") -> Callable[[], bool]:
    """Build a checker for cancellation of the active GACT session.

    A running call asks this every 50 ms, so it reads the session id from the call's
    context without loading the session: with many calls in flight, loading it took
    the session store's lock thousands of times a second. Only an out-of-band call
    with no session in its context resolves one. A view's call outside a turn is
    cancelled through its own request-bound event (:mod:`clio_agent.gact.view_call_cancel`).
    """

    def check() -> bool:
        if view_call_cancel.requested():
            return True
        sid = _ctx.active_tool_session_id().strip() or _resolve_tool_session(app)[0]
        if not sid:
            return False
        event = app.state.cancel_events.get(sid)
        return bool(event is not None and event.is_set()) or sid in app.state.cancel_flags

    return check
