"""Deleting a conversation, shared by ``DELETE /v1/sessions/{sid}`` and workspace removal.

A conversation is deletable once no turn runs and no accepted work in its task tree is
active. Before checking, an admission check in flight is settled and the assignment Stop
held for it is cancelled (:mod:`clio_agent.gact.session_delete_settle`): neither is
running work. Deleting is the person's decision to discard the conversation, so work
that still runs is stopped (:func:`stop_for_delete`, as Stop with ``children: "stop"``)
and no new turn is admitted for it, not even a wake on an obligation or an arriving
task outcome. Only work that does not settle within ``sessions.delete_settle_s`` keeps
the 409. Deleting then removes the conversation's record and everything the service
holds for it, and the workspace's MCP servers stop once no conversation is left to use
them (:func:`close_workspace_fleet`).

Removing a workspace deletes its conversations the same way. It checks every one first
and deletes nothing while any of them still has running work, so the person never ends
up with a removed workspace whose conversations survive without one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from clio_agent.gact.autonomous_loop import stop_session_loop
from clio_agent.gact.compaction import forget_system_prompts
from clio_agent.gact.goal import stop_session_goal
from clio_agent.gact.mcp_apps import cleanup_session_mcp_apps
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.session_delete_settle import settle_held_assignments
from clio_agent.gact.session_processes import close_session_processes
from clio_agent.gact.session_records import PageQuery
from clio_agent.gact.session_tool_output import delete_session_tool_output
from clio_agent.gact.turn_reasoning import forget_session_reasoning
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.lm.session_clients import close_session_model_clients

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

logger = logging.getLogger(__name__)

#: Typed reasons the workspace fleet closes for (logged with the outcome).
REASON_FLEET_SESSION_DELETED = "session_deleted"
REASON_FLEET_WORKSPACE_DELETED = "workspace_deleted"


def _refusal(status_code: int, code: str, message: str, details: dict[str, Any]) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, details=details, recoverable=True)
        ).model_dump(exclude_none=True),
    )


async def active_work(app: Any, sid: str) -> tuple[bool, list[str]]:
    """Whether work still blocks deleting the conversation, and its active task handles.

    Settles first what is not running work: an admission check in flight and the
    assignment Stop held. A running turn blocks deletion even with no active task.
    """
    await app.state.turn_runner.settle_admission(sid)  # an input check is not work
    await settle_held_assignments(app, sid)  # Stop's held assignment is not running work
    active = sorted(app.state.task_history.tree_handles(sid, active_only=True))
    return bool(active) or app.state.turn_runner.busy(sid), active


def delete_settle_s() -> float:
    """How long deleting waits for the work it stopped to settle before refusing (409)."""
    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "sessions.delete_settle_s",
        env="CLIO_SESSION_DELETE_SETTLE_S",
        default=60.0,
        cast=conf.as_float,
    )


async def stop_for_delete(app: Any, deps: "GactDeps", sid: str) -> tuple[bool, list[str]]:
    """Stop everything the conversation runs so it can be deleted; what still blocks it.

    Deleting is the person's decision to discard the conversation, so it stops the turn
    and every task the conversation started, as Stop with ``children: "stop"`` does
    (recorded ``conversation_stop``), and admits no new turn for it meanwhile: a wake
    or a task outcome arriving now does not bring it back. It then waits for that work
    to settle. Work that has not settled by :func:`delete_settle_s` keeps the 409, and
    the conversation's admission reopens so it is usable again.
    """
    import asyncio  # noqa: PLC0415

    from clio_agent.gact.routes.session_cancellation import (  # noqa: PLC0415
        cancel_session_state_async,
    )
    from clio_agent.gact.task_access import TaskCaller  # noqa: PLC0415

    runner = app.state.turn_runner
    runner.retire_admission(sid)
    logger.info("session_delete_stopping_work session=%s", sid)
    await cancel_session_state_async(app, deps, sid, "stop")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delete_settle_s()
    while True:
        blocked, active = await active_work(app, sid)
        remaining = deadline - loop.time()
        if not blocked or remaining <= 0:
            break
        if active:
            caller = TaskCaller("", app.state.human_principal, human_action=True)
            await app.state.task_operations.wait(
                caller, active, "all", timeout_s=min(remaining, 5.0)
            )
        else:
            await asyncio.sleep(min(remaining, 0.2))  # the stopped turn retires
    if blocked:
        runner.reopen_admission(sid)
        logger.warning("session_delete_work_unsettled session=%s handles=%s", sid, ",".join(active))
    return blocked, active


def session_has_active_work(sid: str, handles: list[str]) -> HTTPException:
    """The 409 for a conversation whose work still runs."""
    return _refusal(
        409,
        "session_has_active_work",
        "Cancel and settle active work before deleting this session.",
        {"session_id": sid, "handles": handles},
    )


async def close_workspace_fleet(app: Any, workspace_id: str, reason: str) -> str:
    """Stop the workspace's MCP servers once no conversation is left to use them.

    Its fenced servers otherwise ran on until the idle reaper's TTL, holding the
    workspace's folder (a connector's lock, a port). A conversation still in the
    workspace keeps the shared fleet. Busy servers close at the reaper's next safe
    boundary, never mid-call. Returns the restart outcome, or "" when nothing applied.
    """
    agent = getattr(app.state, "agent", None)
    restart = getattr(agent, "request_fleet_restart", None)
    workspace = app.state.workspaces.get(workspace_id) if workspace_id else None
    root = str(getattr(workspace, "root_path", "") or "")
    if not callable(restart) or not root:
        return ""
    if reason == REASON_FLEET_SESSION_DELETED:
        query = PageQuery(workspace_id=workspace_id, archived=None)
        if await run_off_loop(app.state.sessions.ids, query):
            return ""
    outcome = str(await run_off_loop(restart, root))
    logger.info(
        "workspace_fleet_close_requested workspace=%s reason=%s outcome=%s",
        workspace_id,
        reason,
        outcome,
    )
    return outcome


async def delete_session(app: Any, deps: "GactDeps", sid: str, workspace_id: str) -> bool:
    """Remove a deletable conversation and what the service holds for it.

    Returns whether its record existed. The caller has checked :func:`active_work`.
    """
    # P4.1 #1079 cancel-both: a deleted session must not orphan its loop wakeup.
    stop_session_loop(app, sid)
    stop_session_goal(app, sid)
    try:
        await cleanup_session_mcp_apps(app, sid)
    except RuntimeError as exc:
        raise _refusal(
            502,
            "mcp_app_cleanup_failed",
            "session retained because an owned MCP App failed to close",
            {"session_id": sid, "reason": str(exc)},
        ) from exc
    await run_off_loop(deps.delete_session_messages, app, sid)  # clio-core first: 503 keeps it
    existed = await run_off_loop(app.state.sessions.delete, sid)  # trusted: off-loop only
    if not existed:
        return False
    forget_session_reasoning(app, sid)
    await run_off_loop(close_session_model_clients, app, sid)
    deps.delete_session_context_files(app, sid)
    await run_off_loop(forget_system_prompts, sid)
    await run_off_loop(delete_session_tool_output, app, workspace_id, sid)
    await run_off_loop(close_session_processes, app, sid)  # its kernels and pages (BR23)
    await run_off_loop(deps.release_session_arc, app, sid)  # #1334: drops _events scopes
    await run_off_loop(app.state.task_history.restore)
    app.state.a2ui_catalogs.forget_session(sid)
    app.state.a2ui_store.forget_session(sid)
    return True


async def delete_workspace_sessions(app: Any, deps: "GactDeps", wid: str) -> list[str]:
    """Delete every conversation in the workspace, or none while any still has work.

    Raises a 409 ``workspace_has_active_work`` naming the conversations that hold it.
    Returns the deleted ids, newest first (children before the parents that started them).
    """
    query = PageQuery(workspace_id=wid, archived=None)
    sids = await run_off_loop(app.state.sessions.ids, query)
    busy = {}
    stopped = []
    for sid in sids:
        blocked, handles = await active_work(app, sid)
        if blocked:
            stopped.append(sid)
            blocked, handles = await stop_for_delete(app, deps, sid)
        if blocked:
            busy[sid] = handles
    if busy:
        for sid in stopped:  # nothing is deleted: every conversation stays usable
            app.state.turn_runner.reopen_admission(sid)
        raise _refusal(
            409,
            "workspace_has_active_work",
            "Cancel and settle the work its conversations started before removing this workspace.",
            {"workspace_id": wid, "sessions": busy},
        )
    deleted = []
    for sid in sids:
        if await delete_session(app, deps, sid, wid):
            deleted.append(sid)
    await close_workspace_fleet(app, wid, REASON_FLEET_WORKSPACE_DELETED)
    return deleted


__all__ = [
    "REASON_FLEET_SESSION_DELETED",
    "REASON_FLEET_WORKSPACE_DELETED",
    "active_work",
    "close_workspace_fleet",
    "delete_session",
    "delete_workspace_sessions",
    "session_has_active_work",
    "stop_for_delete",
]
