"""Admission and restart policy for unattended conversation controls.

Schedules survive a backend restart; executing tool effects cannot be replayed safely.
Keep a waiting loop's durable wakeup, but settle interrupted controls explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


async def prepare_scheduled_host(app: Any) -> None:
    """Restore a scheduled session's selected model when no global host was configured."""
    if not hasattr(app.state, "agent") or app.state.agent is not None:
        return
    if getattr(app.state, "lm_config_status", {}).get("state") == "configuring":
        return
    boot = getattr(app.state, "agent_construction_task", None)
    if boot is not None and not boot.done():
        return  # Admission defers until the existing startup build finishes.
    from clio_agent.gact.message_contract import PostMessageRequest
    from clio_agent.gact.session_host_agent import ensure_host_agent, selected_ref

    for schedule in app.state.schedules.list():
        if not schedule.enabled:
            continue
        request = PostMessageRequest(text=schedule.question)
        if selected_ref(app, schedule.session_id, request) is not None:
            await ensure_host_agent(app, schedule.session_id, request)
            return


def admit_loop_wakeup(app: Any, schedule: Any) -> bool:
    """Reject a deleted wakeup or a loop whose limit expired while it was waiting."""
    from clio_agent.gact.autonomous_loop import _LOOP_MUTEX, _check_bounds, _get_loop

    with _LOOP_MUTEX:
        current = app.state.schedules.get(schedule.id)
        if current is None or not current.enabled:
            return False
        loop = _get_loop(app, schedule.session_id)
        if loop.get("pending_schedule_id") != schedule.id:
            return True  # An independent schedule, not this loop's wakeup.
        reason = _check_bounds(app, schedule.session_id, loop)
        if reason:
            stop_loop_and_goal(app, schedule.session_id, reason=reason)
            return False
        from clio_agent.gact.goal import _get_goal

        if expire_goal_budget(app, schedule.session_id, _get_goal(app, schedule.session_id)):
            stop_loop_and_goal(app, schedule.session_id, reason="loop_goal_capped")
            return False
        return bool(loop.get("active") and not loop.get("stopped"))


def settle_failed_loop_schedule(app: Any, schedule: Any) -> None:
    """Expose exhausted scheduling retries as a stopped loop, rather than active work."""
    from clio_agent.gact.autonomous_loop import _LOOP_MUTEX, _get_loop

    with _LOOP_MUTEX:
        current = app.state.schedules.get(schedule.id)
        loop = _get_loop(app, schedule.session_id)
        if (
            current is not None
            and not current.enabled
            and loop.get("pending_schedule_id") == schedule.id
        ):
            stop_loop_and_goal(app, schedule.session_id, reason="loop_schedule_failed")


def stop_loop_and_goal(app: Any, sid: str, *, reason: str) -> None:
    """Stop a loop and its associated goal together, including a pending wakeup."""
    from clio_agent.gact.autonomous_loop import _LOOP_MUTEX, _get_loop, end_loop
    from clio_agent.gact.goal import _get_goal, clear_goal

    with _LOOP_MUTEX:
        loop = _get_loop(app, sid)
        end_loop(app, sid, reason=reason, loop=loop)
        goal = _get_goal(app, sid)
        if goal.get("loop_id") and goal["loop_id"] == loop.get("loop_id"):
            clear_goal(app, sid, reason="goal_loop_stopped")


def bind_goal_to_loop(app: Any, sid: str, loop_id: str) -> None:
    """Associate an armed goal with the loop that controls its pacing and limits."""
    from clio_agent.gact.goal import _GOAL_MUTEX, _get_goal, _put_goal

    with _GOAL_MUTEX:
        goal = _get_goal(app, sid)
        if goal.get("active") and not goal.get("cleared"):
            goal["loop_id"] = loop_id
            _put_goal(app, sid, goal)


def goal_loop_state(app: Any, sid: str, goal: Mapping[str, Any]) -> str:
    """Return immediate/paced/stopped without starting any work."""
    session = app.state.sessions.get(sid)
    loop = (getattr(session, "metadata", None) or {}).get("loop") or {}
    if loop.get("active") and not loop.get("stopped"):
        return "paced"
    if goal.get("loop_id") and goal["loop_id"] == loop.get("loop_id"):
        return "stopped"
    return "immediate"


def goal_redrive_is_current(app: Any, sid: str, metadata: Mapping[str, Any]) -> bool:
    """Discard goal wakeups left behind by a clear, replacement or newly paced loop."""
    if not metadata.get("goal_redrive"):
        return True
    session = app.state.sessions.get(sid)
    goal = (getattr(session, "metadata", None) or {}).get("goal") or {}
    current = bool(
        goal.get("active")
        and not goal.get("cleared")
        and goal.get("goal_id") == metadata.get("goal_id")
        and goal_loop_state(app, sid, goal) == "immediate"
    )
    return current and not expire_goal_budget(app, sid, goal)


def expire_goal_budget(app: Any, sid: str, goal: Mapping[str, Any]) -> bool:
    """Enforce elapsed/token limits before work queued earlier gets another turn."""
    from clio_agent.gact.goal import _GOAL_MUTEX, _budget_spent, _get_goal, clear_goal

    with _GOAL_MUTEX:
        current = _get_goal(app, sid)
        if (
            not current.get("active")
            or current.get("cleared")
            or current.get("goal_id") != goal.get("goal_id")
        ):
            return False
        elapsed, tokens = _budget_spent(app, sid, current)
        wall_limit = float(current.get("max_wallclock_s") or 0)
        token_limit = int(current.get("max_tokens") or 0)
        if (wall_limit > 0 and elapsed >= wall_limit) or (
            token_limit > 0 and tokens >= token_limit
        ):
            clear_goal(app, sid, reason="goal_budget")
            return True
        return False


def reconcile_autonomous_controls(app: Any) -> None:
    """Keep durable waiting wakeups; stop interrupted work instead of replaying effects."""
    from clio_agent.gact.autonomous_loop import _get_loop, end_loop
    from clio_agent.gact.goal import _get_goal, clear_goal

    for session in app.state.sessions.list():
        loop = _get_loop(app, session.id)
        goal = _get_goal(app, session.id)
        if not loop.get("active") and not goal.get("active"):
            continue
        pending = str(loop.get("pending_schedule_id") or "")
        schedule = app.state.schedules.get(pending) if pending else None
        interrupted = session.status in {"running", "error"}
        waiting = bool(schedule is not None and schedule.enabled and not interrupted)
        if loop.get("active") and not waiting:
            end_loop(app, session.id, reason="loop_server_interrupted", loop=loop)
        if goal.get("active") and not (waiting and loop.get("active")):
            clear_goal(app, session.id, reason="goal_server_interrupted")
