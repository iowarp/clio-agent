"""Which background script a call belongs to (taskable run_code CT8), kept import-light.

The permission gate reads this on every question, so it must not import the task layer
or FastMCP (``tests/test_gact/test_startup_imports.py``).
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

__all__ = ["BackgroundTask", "active_background_task", "question_cancel_token"]


@dataclass(frozen=True)
class BackgroundTask:
    """The background script a nested call belongs to: its handle and its stop event."""

    handle: str
    stop: threading.Event


#: Set in a background script's captured context, so its nested calls know their task.
_BACKGROUND_TASK: ContextVar[BackgroundTask | None] = ContextVar(
    "clio_background_task", default=None
)


def active_background_task() -> BackgroundTask | None:
    """The background script the current call belongs to, if any."""
    return _BACKGROUND_TASK.get()


def question_cancel_token(app: Any, session_id: str) -> threading.Event | None:
    """What ends a permission question's wait (CT8): a background script's question ends
    only when its task is cancelled, never on Stop of whatever turn the session runs
    then; any other question ends with its turn, as before."""
    task = _BACKGROUND_TASK.get()
    if task is not None:
        return task.stop
    events = getattr(app.state, "cancel_events", None) or {}
    return events.get(session_id)
