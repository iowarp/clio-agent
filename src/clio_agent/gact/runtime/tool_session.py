"""Session tool bindings and the existing workspace fleet lease."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.runtime import trace


def _tool_workspace_root(sid: str) -> str:
    app_state = getattr(_ctx.active_app(), "state", None)
    sessions = getattr(app_state, "sessions", None)
    workspaces = getattr(app_state, "workspaces", None)
    sess = sessions.get(sid) if sessions is not None else None
    workspace_id = str(getattr(sess, "workspace_id", "") or "")
    workspace = workspaces.get(workspace_id) if workspaces is not None and workspace_id else None
    return str(getattr(workspace, "root_path", "") or "")


def _tree_root(app: Any, sid: str) -> str:
    """The session at the root of ``sid``'s task tree (``sid`` itself for a root)."""

    history = getattr(getattr(app, "state", None), "task_history", None)
    task = history.latest_assignment(sid) if history is not None and sid else None
    while history is not None and task is not None and task.owner.parent_handle:
        parent = history.task_snapshot(task.owner.parent_handle)
        if parent is None:
            break
        task = parent
    return task.owner.session_id if task is not None else sid


async def _finish_fleet_lease_change(change: asyncio.Task[Any]) -> bool:
    """Join an off-loop lease change even when cancellation arrives during it."""
    cancelled = False
    while not change.done():
        try:
            await asyncio.shield(change)
        except asyncio.CancelledError:
            cancelled = True
    change.result()
    return cancelled


@asynccontextmanager
async def _async_tool_session_context(sid: str) -> AsyncIterator[None]:
    """Pin the existing fleet off-loop, keeping tool ContextVars on the caller."""
    root = _tool_workspace_root(sid)
    agent = getattr(getattr(_ctx.active_app(), "state", None), "agent", None)
    lease: Callable[[str], AbstractContextManager[None]] | None = getattr(
        agent, "lease_workspace_fleet", None
    )
    if not root or not callable(lease):
        with _tool_session_context(sid):
            yield
        return
    scope = lease(root)
    entering = asyncio.create_task(asyncio.to_thread(scope.__enter__))
    try:
        await asyncio.shield(entering)
        with _tool_session_context(sid, lease_fleet=False):
            yield
    finally:
        # Cancelling to_thread cannot cancel a blocked lock acquisition. Join it
        # before releasing, including repeated Stop/shutdown cancellation.
        cancelled = await _finish_fleet_lease_change(entering)
        leaving = asyncio.create_task(asyncio.to_thread(scope.__exit__, None, None, None))
        cancelled |= await _finish_fleet_lease_change(leaving)
        if cancelled:
            raise asyncio.CancelledError


@contextmanager
def _tool_session_context(sid: str, *, lease_fleet: bool = True) -> Iterator[None]:
    """Bind the tool session id + workspace root + plugin MCP server set for the turn.

    The four tool-runtime hooks are resolved per tool call by the installed
    ``ToolRuntimeHooks`` resolver (``resolve_tool_runtime`` dispatching on the
    keystone-bound ``_ctx.active_app()``), so this no longer binds them. It binds
    the tool session id (read by the permission gate to attribute the call to
    the live turn's session), the workspace root — resolved off the live app so
    the tool executor grounds output artifacts into the bound workspace (#735) —
    and the session's plugin MCP server set (#1232 pt 1), so
    ``ClioAgent._active_tool_executor`` mounts exactly the servers of the
    session's resolved plugins into the per-workspace gateway, never every
    installed plugin's servers and never at boot.
    """
    from clio_agent.gact.agents.resolution import session_server_set  # noqa: PLC0415
    from clio_agent.gact.node_folders import session_node_folder  # noqa: PLC0415
    from clio_agent.tools.execution import (  # noqa: PLC0415
        tool_server_set_context,
        tool_workspace_context,
    )
    from clio_agent.tools.write_scope import tool_write_root_context  # noqa: PLC0415

    workspace_root = _tool_workspace_root(sid)
    app = _ctx.active_app()
    node_folder = session_node_folder(app, sid)
    from clio_agent.tools.caller_scope import tool_caller_context  # noqa: PLC0415

    caller = tool_caller_context(sid, _tree_root(app, sid))
    app_state = getattr(app, "state", None) if app is not None else None
    server_set = session_server_set(app, sid) if app is not None else ""
    token = _ctx.set_tool_session_id(sid)
    # #933: pin the workspace fleet for the WHOLE turn — between-call idleness
    # inside a live turn must not count toward the reaper's TTL.
    agent = getattr(app_state, "agent", None) if app_state is not None else None
    lease: Callable[[str], AbstractContextManager[None]] | None = getattr(
        agent, "lease_workspace_fleet", None
    )
    try:
        if lease_fleet and workspace_root and callable(lease):
            with (
                lease(workspace_root),
                tool_workspace_context(workspace_root),
                tool_write_root_context(node_folder),
                caller,
                tool_server_set_context(server_set),
            ):
                yield
        else:
            if lease_fleet and workspace_root:
                # A rooted turn without a leasable agent runs UNPROTECTED from
                # the #933 reaper — degraded path, so the reason is typed.
                trace.event(
                    "TOOLS",
                    "workspace_lease_unavailable session=%s root=%s reason=agent_has_no_lease_hook",
                    sid,
                    workspace_root,
                )
            with (
                tool_workspace_context(workspace_root),
                tool_write_root_context(node_folder),
                caller,
                tool_server_set_context(server_set),
            ):
                yield
    finally:
        _ctx.reset(token)
