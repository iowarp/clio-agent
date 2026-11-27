"""A view's slow call answers ``202`` with a status location, so it never holds a connection.

Mounting a view, reopening one and a view's own ``tools/call`` or resource read can wait
for minutes: the server is spawned or reconnected, and a ParaView node half waits up to
120 s for its pvserver, one call after another. Each such request held one HTTP/1.1
connection the whole time. A browser opens at most six per origin, so a conversation
with a few views reopening used every connection: the page's own reads (messages,
interactions, ``/v1/health``) queued behind them and the UI looked hung while the agent
answered everyone else at once (take 1t).

A client that sends ``Prefer: respond-async`` (RFC 7240, optionally with ``wait=N``)
gets the answer when it is ready within that wait, as before. Otherwise the call keeps
running on the server and the request answers ``202`` at once with::

    {"view_call_pending": {"id": ..., "location": ..., "route": ..., "retry_after_ms": ...}}

``GET location`` answers the same ``202`` while the call runs, and then exactly what the
original request would have answered (its result, or its error status and body).
``DELETE location`` cancels the call. Each poll renews the call's lease: when nobody has
polled for :data:`LEASE_S` seconds the page is gone, and the call is cancelled exactly as
a dropped request cancels it (:mod:`.view_call_cancel`). A client without the header
keeps the held request.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

PENDING_KEY = "view_call_pending"
PENDING_UNKNOWN = "mcp_app_pending_unknown"
#: How long a request waits for the answer before it answers ``202`` (unless ``wait=N``).
DEFAULT_WAIT_S = 2.0
MAX_WAIT_S = 10.0
#: A pending call nobody polled for this long is cancelled (its page went away).
LEASE_S = 20.0
RETRY_AFTER_MS = 1000


def preferred_wait(request: Any) -> float | None:
    """The ``Prefer: respond-async`` wait in seconds, or ``None`` without the preference."""

    headers = getattr(request, "headers", None)
    raw = headers.get("prefer", "") if headers is not None else ""
    tokens = [token.strip().lower() for token in raw.split(",") if token.strip()]
    if "respond-async" not in tokens:
        return None
    wait = DEFAULT_WAIT_S
    for token in tokens:
        name, _, value = token.partition("=")
        if name.strip() == "wait":
            try:
                wait = float(value.strip())
            except ValueError:
                continue
    return min(max(wait, 0.0), MAX_WAIT_S)


class _Lease:
    """Stands in for the request that asked: "disconnected" once the call is abandoned."""

    def __init__(self, request: Any) -> None:
        self._request: Any = request
        self.polled = time.monotonic()
        self.cancelled = False

    def detach(self) -> None:
        self._request = None  # answered 202: the original connection is closed now
        self.polled = time.monotonic()

    def renew(self) -> None:
        self.polled = time.monotonic()

    async def is_disconnected(self) -> bool:
        if self.cancelled:
            return True
        if self._request is not None:
            return bool(await self._request.is_disconnected())
        if time.monotonic() - self.polled > LEASE_S:
            logger.info("%s reason=lease_expired", PENDING_KEY)
            self.cancelled = True
        return self.cancelled


@dataclass
class _Pending:
    task: asyncio.Future[Any]
    session_id: str
    route: str
    lease: _Lease
    done_at: float | None = field(default=None)


def _registry(app: FastAPI) -> dict[str, _Pending]:
    pending = getattr(app.state, "view_calls_pending", None)
    if not isinstance(pending, dict):
        pending = app.state.view_calls_pending = {}
    return pending


def _sweep(app: FastAPI) -> None:
    """Drop answers nobody collected within a lease (the page went away)."""

    now = time.monotonic()
    pending = _registry(app)
    for pid, entry in list(pending.items()):
        if entry.done_at is not None and now - entry.done_at > LEASE_S:
            pending.pop(pid, None)
            if not entry.task.cancelled():
                entry.task.exception()  # retrieved: an uncollected failure is not "never retrieved"


def _location(sid: str, pid: str) -> str:
    return f"/v1/sessions/{sid}/mcp-apps/pending/{pid}"


def _pending_answer(sid: str, pid: str, route: str) -> JSONResponse:
    location = _location(sid, pid)
    return JSONResponse(
        {
            PENDING_KEY: {
                "id": pid,
                "location": location,
                "route": route,
                "retry_after_ms": RETRY_AFTER_MS,
            }
        },
        status_code=202,
        headers={
            "Location": location,
            "Retry-After": str(max(1, RETRY_AFTER_MS // 1000)),
            "Preference-Applied": "respond-async",
            "Cache-Control": "no-store",
        },
    )


async def respond(
    app: FastAPI,
    request: Request,
    sid: str,
    route: str,
    operation: Callable[[Any], Awaitable[Any]],
) -> Any:
    """Answer ``operation(cancel_source)`` now, or ``202`` with its status location.

    ``cancel_source`` is what the call is bound to for cancellation
    (:func:`view_call_cancel.run`'s ``request``): the request itself when the client
    holds it, else a lease the polls renew.
    """

    wait = preferred_wait(request)
    if wait is None:
        return await operation(request)
    lease = _Lease(request)
    task = asyncio.ensure_future(operation(lease))
    try:
        done, _ = await asyncio.wait({task}, timeout=wait)
    except asyncio.CancelledError:
        lease.cancelled = True
        raise
    if task in done:
        return task.result()
    _sweep(app)
    pid = secrets.token_urlsafe(12)
    entry = _Pending(task=task, session_id=sid, route=route, lease=lease)
    lease.detach()

    def settled(_: asyncio.Future[Any]) -> None:
        entry.done_at = time.monotonic()

    task.add_done_callback(settled)
    _registry(app)[pid] = entry
    logger.info("%s route=%s session=%s id=%s", PENDING_KEY, route, sid, pid)
    return _pending_answer(sid, pid, route)


def _unknown() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": PENDING_UNKNOWN,
            "message": "This view call is not pending here: it was collected, cancelled "
            "or forgotten (CLIO restarted). Ask again.",
        },
    )


def register_view_pending_routes(app: FastAPI) -> None:
    """``GET``/``DELETE /v1/sessions/{sid}/mcp-apps/pending/{pid}``."""

    def entry_of(sid: str, pid: str) -> _Pending:
        entry = _registry(app).get(pid)
        if entry is None or entry.session_id != sid:
            raise _unknown()
        return entry

    @app.get("/v1/sessions/{sid}/mcp-apps/pending/{pid}")
    async def get_pending_view_call(sid: str, pid: str) -> Any:
        _sweep(app)
        entry = entry_of(sid, pid)
        entry.lease.renew()
        if not entry.task.done():
            return _pending_answer(sid, pid, entry.route)
        _registry(app).pop(pid, None)
        return entry.task.result()  # its error raises here, as the original would have

    @app.delete("/v1/sessions/{sid}/mcp-apps/pending/{pid}")
    async def cancel_pending_view_call(sid: str, pid: str) -> dict[str, Any]:
        entry = entry_of(sid, pid)
        entry.lease.cancelled = True  # the call is cancelled as a dropped request's call
        if entry.task.done():
            _registry(app).pop(pid, None)
        return {"id": pid, "cancelled": True}


def cancel_all(app: FastAPI) -> int:
    """Cancel every pending view call (CLIO is shutting down)."""

    entries = list(_registry(app).values())
    for entry in entries:
        entry.lease.cancelled = True
        entry.task.cancel()
    _registry(app).clear()
    return len(entries)


__all__ = [
    "DEFAULT_WAIT_S",
    "LEASE_S",
    "PENDING_KEY",
    "PENDING_UNKNOWN",
    "cancel_all",
    "preferred_wait",
    "register_view_pending_routes",
    "respond",
]
