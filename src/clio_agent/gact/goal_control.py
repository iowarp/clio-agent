"""Goal launch and agent control, separate from completion judgement."""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.goal import GoalDecision, GoalError, _enqueue_goal_redrive, _get_goal, arm_goal


def start_goal(app: Any, sid: str, *, condition: str, **limits: Any) -> dict[str, Any]:
    """Set a goal and queue its first turn when the conversation is idle."""
    result = arm_goal(app, sid, condition=condition, **limits)
    runner = getattr(app.state, "turn_runner", None)
    if runner is None or not runner.busy(sid):
        goal = _get_goal(app, sid)
        _enqueue_goal_redrive(
            app, sid, GoalDecision(outcome="redrive", reason="goal_started", new_state=goal), goal
        )
    return result


async def launch_pending_goal(app: Any, sid: str) -> None:
    """Prepare the conversation's model and drain an explicitly started goal."""
    from clio_agent.gact.loop_inbox import drain_inbox_to_new_turn
    from clio_agent.gact.message_contract import PostMessageRequest
    from clio_agent.gact.session_host_agent import ensure_host_agent

    await ensure_host_agent(app, sid, PostMessageRequest(text="Continue the active goal"))
    app.state.sessions.update(sid, metadata_patch={"composer_queue_paused": False})
    drain_inbox_to_new_turn(app, sid)


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


async def maintain_goal_controls(app: Any) -> None:
    """Enforce explicit waiting budgets and launch retained recovery inboxes."""
    from clio_agent.gact.autonomy_guard import expire_goal_budget
    from clio_agent.gact.loop_inbox import drain_inbox_to_new_turn
    from clio_agent.gact.off_loop import run_off_loop

    for session in app.state.sessions.list():
        goal = _get_goal(app, session.id)
        if not goal.get("active"):
            continue
        expired = await run_off_loop(expire_goal_budget, app, session.id, goal)
        if (
            not expired
            and goal.get("phase") == "recovering"
            and getattr(app.state, "agent", None) is not None
        ):
            drain_inbox_to_new_turn(app, session.id)


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
