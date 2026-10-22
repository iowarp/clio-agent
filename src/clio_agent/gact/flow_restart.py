"""Restarting a started flow restarts its run, whichever of its handles is named.

A flow started through ``run_flow``/``run_workflow``/``ctx.call`` runs in its own
session. Input that reaches that session after the run ended opens a later
assignment there which only passes the input on (``flows.runner``), so the
handle a person or agent sees for the flow is soon not the session's latest.
A restart aimed at any of the session's assignments applies to the latest one,
and the flow runner resumes the last run that recorded work.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Coroutine
from typing import Any

Restart = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


def started_flow(metadata: Any) -> bool:
    """Whether a session's metadata marks it as running a started flow."""

    flow = (metadata or {}).get("agent_flow") if isinstance(metadata, dict) else None
    return isinstance(flow, dict) and (flow.get("input") is not None or "depth" in flow)


def restart_target(conversations: Any, handle: str) -> str:
    """The assignment a restart of ``handle`` applies to."""

    history = conversations.history
    task = history.task_snapshot(handle)
    if task is None:
        return handle
    session = conversations.sessions.get(task.owner.session_id)
    if not started_flow(getattr(session, "metadata", None)):
        return handle
    latest = history.latest_assignment(task.owner.session_id)
    return latest.handle if latest is not None and latest.terminal_at is not None else handle


def to_latest_flow_assignment(restart: Restart) -> Restart:
    """Wrap ``Conversations.restart`` so a flow's restart targets its latest assignment."""

    @functools.wraps(restart)
    async def run(self: Any, caller: Any, handle: str) -> dict[str, Any]:
        target = await self.history.io.run(restart_target, self, handle)
        return await restart(self, caller, target)

    return run


__all__ = ["restart_target", "started_flow", "to_latest_flow_assignment"]
