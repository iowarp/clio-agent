"""Goal launch and agent control, separate from completion judgement."""

from __future__ import annotations

import asyncio


def build_goal_wait_tool() -> Any:
    """Declare goal dependencies without leaving a dormant assignment's worker parked."""
    from clio_agent.gact import context
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    def wait_for(tasks: str | list[str], return_when: str = "all") -> dict[str, Any]:
        app = context.active_app()
        if app is None:
            raise ValueError("Goal dependencies require an active product conversation")
        return app.state.conversations.set_dependencies(
            context.active_session_id(), tasks, return_when
        )

    return native_tool(
        wait_for,
        name="wait_for",
        args={
            "tasks": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}, "minItems": 1},
                ]
            },
            "return_when": {"type": "string", "enum": ["any", "all"], "default": "all"},
        },
        desc="Declare the current goal's any/all task dependencies, then yield. Partial results remain queued until this condition; human feedback and Question answers still arrive. Use wait_tasks for an explicit active wait.",
        presentation="wait",
        title="Await goal dependencies",
        domain="goals",
    )


from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.goal import GoalDecision, GoalError, _enqueue_goal_redrive, _get_goal, arm_goal


def start_goal(app: Any, sid: str, *, condition: str, **limits: Any) -> dict[str, Any]:
    """Set a goal and queue its first turn when the conversation is idle."""
    result = arm_goal(app, sid, condition=condition, **limits)
    goal = _get_goal(app, sid)
    _enqueue_goal_redrive(
        app, sid, GoalDecision(outcome="redrive", reason="goal_started", new_state=goal), goal
    )
    return result


async def launch_pending_goal(app: Any, sid: str) -> None:
    """Prepare the selected model and signal accepted Core input without spawning a turn."""
    from clio_agent.gact.session_host_agent import ensure_host_agent
    from clio_agent.gact.types import PostMessageRequest

    await ensure_host_agent(app, sid, PostMessageRequest(text="Continue the active goal"))
    app.state.conversations.signal_pending()


async def maintain_goal_controls(app: Any) -> None:
    """Keep explicit goal budgets, leaving recovery and admission with the conversation owner."""
    from clio_agent.gact.autonomy_guard import expire_goal_budget

    def maintain() -> None:
        for session in app.state.sessions.list():
            goal = _get_goal(app, session.id)
            if goal.get("active"):
                expire_goal_budget(app, session.id, goal)

    await asyncio.to_thread(maintain)
    # The first scheduler tick can run before boot has built the conversation runtime;
    # there is nothing pending to signal yet, and the next tick signals as usual.
    conversations = getattr(app.state, "conversations", None)
    if conversations is not None:
        conversations.signal_pending()


def pause_failed_goal(app: Any, sid: str, reason: str) -> None:
    """Retain an interrupted objective for inspection and explicit user restart."""
    from clio_agent.gact.goal import _GOAL_MUTEX, _put_goal

    with _GOAL_MUTEX:
        goal = _get_goal(app, sid)
        if goal.get("active"):
            goal.update(
                active=False, paused=True, cleared=False, phase="paused", clear_reason=reason
            )
            _put_goal(app, sid, goal)


def retain_shutdown_goal(app: Any, sid: str) -> None:
    """Keep an interrupted objective recoverable without reviving a user stop."""
    from clio_agent.gact.goal import _GOAL_MUTEX, _put_goal

    with _GOAL_MUTEX:
        goal = _get_goal(app, sid)
        if goal.get("active") and not goal.get("cleared"):
            goal.update(phase="recovering", clear_reason="goal_server_interrupted")
            _put_goal(app, sid, goal)


def build_set_goal_tool() -> Any:
    """Build the agent's goal setter without giving it authority to declare completion."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    def set_goal(
        condition: str,
        max_goal_iters: int = 0,
        max_wallclock_s: float = 0,
        max_tokens: int = 0,
    ) -> dict[str, Any]:
        """Start working toward a long-term objective. Zero limits mean unlimited.

        Use when the user asks for sustained autonomous work. Continue working now;
        an independent judge checks completion after each turn and supplies guidance.
        Existing active goals and explicit user stops cannot be overwritten by this tool.
        Only the user can restart or replace those. Loops and schedules are independent.
        """
        app, sid = _ctx.active_app(), _ctx.active_session_id()
        if app is None or not sid:
            raise GoalError("No active conversation", reason="no_active_session")
        current = _get_goal(app, sid)
        if current and not current.get("met"):
            raise GoalError(
                "Ask the user to replace or restart the existing goal", reason="goal_protected"
            )
        # The current agent turn is already the first work turn; do not queue a duplicate.
        return arm_goal(
            app,
            sid,
            condition=condition,
            max_goal_iters=max_goal_iters,
            max_wallclock_s=max_wallclock_s,
            max_tokens=max_tokens,
        )

    return native_tool(
        set_goal,
        name="set_goal",
        presentation="goal_started",
        domain="autonomy",
        desc=set_goal.__doc__,
        title="Start goal",
        args={
            "condition": {"type": "string"},
            "max_goal_iters": {"type": "integer"},
            "max_wallclock_s": {"type": "number"},
            "max_tokens": {"type": "integer"},
        },
    )
