"""Deleting a conversation, shared by ``DELETE /v1/sessions/{sid}`` and workspace removal.

A conversation is deletable once no turn runs and no accepted work in its task tree is
active. Before checking, an admission check in flight is settled and the assignment Stop
held for it is cancelled (:mod:`clio_agent.gact.session_delete_settle`): neither is
running work. Deleting then removes the conversation's record and everything the
service holds for it.

Removing a workspace deletes its conversations the same way. It checks every one first
and deletes nothing while any of them still has running work, so the person never ends
up with a removed workspace whose conversations survive without one.
"""

from __future__ import annotations

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


def session_has_active_work(sid: str, handles: list[str]) -> HTTPException:
    """The 409 for a conversation whose work still runs."""
    return _refusal(
        409,
        "session_has_active_work",
        "Cancel and settle active work before deleting this session.",
        {"session_id": sid, "handles": handles},
    )


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
    for sid in sids:
        blocked, handles = await active_work(app, sid)
        if blocked:
            busy[sid] = handles
    if busy:
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
    return deleted


__all__ = [
    "active_work",
    "delete_session",
    "delete_workspace_sessions",
    "session_has_active_work",
]
