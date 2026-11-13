"""A view's call outside a turn is cancellable: it is bound to the request that asked.

A call a view makes (``tools/call``, a resource read) or a reopen replays runs outside
any turn, so the session's Cancel never reaches it. Each such call is bound to a cancel
event instead, set when the request that asked for it goes away (the page was closed or
reloaded and the connection dropped), when its awaiting task is cancelled, or when CLIO
shuts down (:func:`cancel_all`). The session's cancellation checker
(:mod:`clio_agent.gact.cancellation_check`) reads it, so the call is cancelled exactly as
a turn's Cancel cancels a turn's call, and it fails with the typed reason
:data:`VIEW_CALL_CANCELLED`. There is no timer: a call that is still wanted is waited for.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

VIEW_CALL_CANCELLED = "mcp_app_call_cancelled"
#: What the call's task record says when its view's request went away mid-call.
VIEW_CALL_CANCELLED_TEXT = (
    f"{VIEW_CALL_CANCELLED}: the view's request went away before the service answered; "
    "the call was cancelled and whether the service acted on it is not known."
)
#: How often an in-flight call looks whether its request is still connected.
DISCONNECT_POLL_S = 0.5

_EVENT: ContextVar[threading.Event | None] = ContextVar("clio_view_call_cancel", default=None)


def requested() -> bool:
    """Whether the view call running in this context was cancelled."""

    event = _EVENT.get()
    return event is not None and event.is_set()


def bound_here() -> bool:
    """Whether this context runs a view call outside a turn."""

    return _EVENT.get() is not None


@contextmanager
def bound(event: threading.Event) -> Iterator[None]:
    """Bind ``event`` as the cancel of the view call run in this context."""

    token = _EVENT.set(event)
    try:
        yield
    finally:
        _EVENT.reset(token)


def _live_events(app: Any) -> set[threading.Event]:
    events = getattr(app.state, "view_call_cancels", None)
    if not isinstance(events, set):
        events = set()
        app.state.view_call_cancels = events
    return events


def cancel_all(app: Any) -> int:
    """Cancel every view call in flight (CLIO is shutting down). Returns how many."""

    events = tuple(_live_events(app))
    for event in events:
        event.set()
    if events:
        logger.info("%s reason=shutdown calls=%d", VIEW_CALL_CANCELLED, len(events))
    return len(events)


async def _watch(request: Any, event: threading.Event) -> None:
    while not event.is_set():
        if await request.is_disconnected():
            logger.info("%s reason=request_disconnected", VIEW_CALL_CANCELLED)
            event.set()
            return
        await asyncio.sleep(DISCONNECT_POLL_S)


async def run(app: Any, invoke: Callable[[], Any], *, request: Any = None) -> Any:
    """Run the blocking ``invoke`` on a worker thread, cancellable as described above."""

    event = threading.Event()

    def call() -> Any:
        with bound(event):
            return invoke()

    live = _live_events(app)
    live.add(event)
    work = asyncio.ensure_future(asyncio.to_thread(call))
    watcher = asyncio.ensure_future(_watch(request, event)) if request is not None else None
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        logger.info("%s reason=caller_cancelled", VIEW_CALL_CANCELLED)
        event.set()
        raise
    finally:
        if watcher is not None:
            watcher.cancel()
        live.discard(event)


__all__ = [
    "DISCONNECT_POLL_S",
    "VIEW_CALL_CANCELLED",
    "VIEW_CALL_CANCELLED_TEXT",
    "bound",
    "bound_here",
    "cancel_all",
    "requested",
    "run",
]
