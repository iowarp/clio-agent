"""Cancellation checks shared by the GACT tool-execution boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from clio_agent.gact.runtime.globals import _resolve_tool_session

if TYPE_CHECKING:
    from fastapi import FastAPI


def make_cancellation_checker(app: "FastAPI") -> Callable[[], bool]:
    """Build a checker for cancellation of the active GACT session."""

    def check() -> bool:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return False
        event = app.state.cancel_events.get(sid)
        return bool(event is not None and event.is_set()) or sid in app.state.cancel_flags

    return check
