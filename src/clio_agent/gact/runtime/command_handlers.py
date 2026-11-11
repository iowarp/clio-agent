"""The handlers the core plugin's built-in command files name with ``handler:``.

A built-in command is a file of the core plugin (``ai.iowarp.clio/commands``)
whose ``handler:`` names one of these entries, the way ``native_tool`` claims a
built-in tool. Each handler lives with the feature it drives (the loop, goal and
cron owners), and returns the text of the command's result message. ``after``
runs once that message is in the conversation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from clio_agent.gact.events import Event
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.routes.deps import GactDeps
    from clio_agent.gact.types import Message, Session

Run = Callable[["FastAPI", "GactDeps", "Session", Mapping[str, Any]], Awaitable[str]]
After = Callable[["FastAPI", str, "Message"], Awaitable[None]]


@dataclass(frozen=True)
class CommandHandler:
    run: Run
    after: After | None = None


async def _clear(app: "FastAPI", deps: "GactDeps", sess: "Session", body: Mapping[str, Any]) -> str:
    sid = sess.id
    deps.guard_direct_destructive_action(
        app,
        session_id=sid,
        workspace_id=sess.workspace_id,
        tool_name="gact.session.clear",
        args={"session_id": sid, "command": "/clear"},
        summary=f"clear session messages for {sid}",
        reason="user_requested_session_clear",
    )
    await run_off_loop(deps.delete_session_messages, app, sid)
    app.state.sessions.update(sid, message_count=0)
    app.state.bus.publish(
        Event(type="session.cleared", session_id=sid, payload={"session_id": sid})
    )
    return "session messages cleared"


async def _cache_stats(
    app: "FastAPI", deps: "GactDeps", sess: "Session", body: Mapping[str, Any]
) -> str:
    stats: dict[str, Any] = {}
    if app.state.arc is not None:
        try:
            stats = app.state.arc.get_cache_stats() or {}
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="command_error",
                        message="Backend command /cache-stats could not read ARC cache statistics.",
                        details={
                            "command": "/cache-stats",
                            "original_error": str(exc),
                            "recovery_actions": ["retry", "reconfigure_provider", "exit"],
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
    return (
        f"ARC cache: hits={stats.get('hits', 0)} "
        f"misses={stats.get('misses', 0)} "
        f"hit_rate={stats.get('hit_rate', 0.0):.2f} "
        f"capacity={stats.get('capacity', 0)}"
    )


async def _dump_trace(
    app: "FastAPI", deps: "GactDeps", sess: "Session", body: Mapping[str, Any]
) -> str:
    log = app.state.messages.get(sess.id, [])
    last = next((m for m in reversed(log) if m.role == "assistant"), None)
    if last is None:
        return "no assistant turns yet"
    thinking = next((p for p in last.parts if p.type == "thinking"), None)
    return thinking.text if thinking is not None else "no thinking trace on the last turn"


async def _loop(app: "FastAPI", deps: "GactDeps", sess: "Session", body: Mapping[str, Any]) -> str:
    from clio_agent.gact.autonomous_loop import run_loop_command  # noqa: PLC0415

    return run_loop_command(app, sess.id, dict(body))


async def _goal(app: "FastAPI", deps: "GactDeps", sess: "Session", body: Mapping[str, Any]) -> str:
    from clio_agent.gact.goal import run_goal_command  # noqa: PLC0415

    return await app.state.task_history.io.run(run_goal_command, app, sess.id, dict(body))


async def _goal_after(app: "FastAPI", sid: str, message: "Message") -> None:
    if message.parts and message.parts[0].code == "goal_started":
        from clio_agent.gact.goal_control import launch_pending_goal  # noqa: PLC0415

        await launch_pending_goal(app, sid)


async def _cron(app: "FastAPI", deps: "GactDeps", sess: "Session", body: Mapping[str, Any]) -> str:
    from clio_agent.gact.cron_tools import run_cron_command  # noqa: PLC0415

    return run_cron_command(app, sess.id, dict(body))


HANDLERS: dict[str, CommandHandler] = {
    "clear": CommandHandler(_clear),
    "cache-stats": CommandHandler(_cache_stats),
    "dump-trace": CommandHandler(_dump_trace),
    "loop": CommandHandler(_loop),
    "goal": CommandHandler(_goal, after=_goal_after),
    "cron": CommandHandler(_cron),
}


__all__ = ["HANDLERS", "CommandHandler"]
