"""A conversation on screen keeps its prepared plugin MCP servers (the runtime model).

The server's memory is a bounded cache whose items stay while a view or a running
obligation uses them. A conversation's plugin MCP servers live in its workspace fleet,
which the workspace reaper (:class:`~clio_agent.tools.reaper.WorkspaceExecutorReaper`)
closes after ``tools.mcp.workspace_ttl_s`` without a tool call -- so a person reading a
conversation for two minutes saw its servers drop to "Starts when used". A view is a
live SSE subscriber on the conversation (``GET /v1/sessions/{sid}/events``):

* the first view of a session takes a lease on its workspace fleet (the same lease a
  running turn or a session waiting on its user holds), so the reaper keeps it;
* the last view to go releases it and starts a fresh idle window (the reaper's
  ``note_resolved``), so the fleet then leaves on the normal idle path: one
  ``workspace_ttl_s`` after the view went, not at the next reap tick.

Both steps run off the server loop (the session row is a clio-core read and the fleet
lock can be held while a fleet is built). A lease refused while connected-data access
is being reconfigured is logged with a typed reason and the view goes on unheld.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Typed reasons this module logs (no silent fallback).
VIEW_HOLD_REFUSED = "view_fleet_hold_refused"
VIEW_HOLD_FAILED = "view_fleet_hold_failed"

__all__ = ["VIEW_HOLD_FAILED", "VIEW_HOLD_REFUSED", "held_views", "viewing"]


@dataclass
class _Hold:
    views: int
    agent: Any = None
    root: str = ""
    lease: AbstractContextManager[None] | None = None


_holds: dict[str, _Hold] = {}
_lock = threading.Lock()


def held_views() -> dict[str, int]:
    """``{session id: live views}`` of the sessions whose fleet a view holds."""
    with _lock:
        return {sid: hold.views for sid, hold in _holds.items() if hold.lease is not None}


def _fleet_root(app: Any, sid: str) -> str:
    """The session's workspace root (reads the session row: call off the loop)."""
    state = getattr(app, "state", None)
    sessions = getattr(state, "sessions", None)
    workspaces = getattr(state, "workspaces", None)
    session = sessions.get(sid) if sessions is not None else None
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    workspace = workspaces.get(workspace_id) if workspaces is not None and workspace_id else None
    return str(getattr(workspace, "root_path", "") or "")


def _acquire(app: Any, sid: str) -> None:
    with _lock:
        hold = _holds.setdefault(sid, _Hold(views=0))
        hold.views += 1
        if hold.views > 1:
            return
    agent = getattr(getattr(app, "state", None), "agent", None)
    lease = getattr(agent, "lease_workspace_fleet", None)
    root = _fleet_root(app, sid)
    if not root or not callable(lease):
        return
    scope = lease(root)
    try:
        scope.__enter__()
    except RuntimeError as error:  # connected-data access is being reconfigured
        logger.warning("%s session=%s root=%s error=%s", VIEW_HOLD_REFUSED, sid, root, error)
        return
    with _lock:
        hold = _holds.get(sid)
        if hold is not None and hold.views > 0 and hold.lease is None:
            hold.agent, hold.root, hold.lease = agent, root, scope
            scope = None
    if scope is not None:  # every view left while the lease was being taken
        scope.__exit__(None, None, None)
        return
    logger.info("view_fleet_held session=%s root=%s", sid, root)


def _release(sid: str) -> None:
    with _lock:
        hold = _holds.get(sid)
        if hold is None:
            return
        hold.views -= 1
        if hold.views > 0:
            return
        del _holds[sid]
    if hold.lease is None:
        return
    hold.lease.__exit__(None, None, None)
    _restart_idle_window(hold.agent, hold.root)
    logger.info("view_fleet_released session=%s root=%s", sid, hold.root)


def _restart_idle_window(agent: Any, root: str) -> None:
    """The fleet's idle time counts from when the last view went (reaper TTL)."""
    from clio_agent.tools.workspace_root import canonical_workspace_root  # noqa: PLC0415

    reaper = getattr(agent, "_workspace_reaper", None)
    state = getattr(agent, "_workspace_state", None)
    if reaper is None or not callable(state):
        return
    lock, _executors, _leases = state()
    with lock:
        reaper.note_resolved(canonical_workspace_root(root))


def _run_logged(step: Any, *args: Any) -> None:
    try:
        step(*args)
    except Exception:  # noqa: BLE001 - logged typed; a view never fails over its fleet hold
        logger.exception("%s step=%s", VIEW_HOLD_FAILED, getattr(step, "__name__", step))


@asynccontextmanager
async def viewing(app: Any, sid: str) -> AsyncIterator[None]:
    """Hold ``sid``'s workspace fleet for the duration of one view (an SSE stream).

    The acquire is awaited off the loop; the release is handed to a worker thread and
    not awaited, so a disconnecting client's cancellation never blocks on it.
    """
    loop = asyncio.get_running_loop()
    acquired = loop.run_in_executor(None, _run_logged, _acquire, app, sid)
    try:
        await asyncio.shield(acquired)
        yield
    finally:
        # After the acquire landed (it counted this view even if the client left first).
        acquired.add_done_callback(
            lambda _done: loop.run_in_executor(None, _run_logged, _release, sid)
        )
