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
    for session in app.state.sessions.list():
        if (session.metadata or {}).get("goal", {}).get("active"):
            request = PostMessageRequest(text="Recover the active goal")
            if selected_ref(app, session.id, request) is not None:
                await ensure_host_agent(app, session.id, request)
                return


def admit_loop_wakeup(app: Any, schedule: Any) -> bool:
    """Reject a deleted wakeup or a loop whose limit expired while it was waiting."""
    from clio_agent.gact.autonomous_loop import _LOOP_MUTEX, _check_bounds, _get_loop, end_loop

    with _LOOP_MUTEX:
        current = app.state.schedules.get(schedule.id)
        if current is None or not current.enabled:
            return False
        loop = _get_loop(app, schedule.session_id)
        if loop.get("pending_schedule_id") != schedule.id:
            return True  # An independent schedule, not this loop's wakeup.
        reason = _check_bounds(app, schedule.session_id, loop)
        if reason:
            end_loop(app, schedule.session_id, reason=reason, loop=loop)
            return False
        return bool(loop.get("active") and not loop.get("stopped"))


def settle_failed_loop_schedule(app: Any, schedule: Any) -> None:
    """Expose exhausted scheduling retries as a stopped loop, rather than active work."""
    from clio_agent.gact.autonomous_loop import _LOOP_MUTEX, _get_loop, end_loop

    with _LOOP_MUTEX:
        current = app.state.schedules.get(schedule.id)
        loop = _get_loop(app, schedule.session_id)
        if (
            current is not None
            and not current.enabled
            and loop.get("pending_schedule_id") == schedule.id
        ):
            end_loop(app, schedule.session_id, reason="loop_schedule_failed", loop=loop)


def goal_redrive_is_current(app: Any, sid: str, metadata: Mapping[str, Any]) -> bool:
    """Discard stale goal wakeups and yield to accepted human input."""
    from clio_agent.gact.goal_steering import pending_steer

    if not metadata.get("goal_redrive"):
        return True
    session = app.state.sessions.get(sid)
    goal = (getattr(session, "metadata", None) or {}).get("goal") or {}
    current = bool(
        goal.get("active")
        and not goal.get("cleared")
        and goal.get("goal_id") == metadata.get("goal_id")
        and (
            "goal_iters" not in metadata
            or metadata["goal_iters"] == int(goal.get("iters_elapsed", 0) or 0)
        )
    )
    return current and not expire_goal_budget(app, sid, goal) and not pending_steer(app, sid)


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
    from clio_agent.gact.goal import GoalDecision, _enqueue_goal_redrive, _get_goal, _put_goal
    from clio_agent.gact.goal_wait import waiting_on_tasks

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
        if goal.get("active"):
            if goal.get("phase") == "needs_input" or session.status in {
                "waiting_user",
                "waiting_permission",
            }:
                continue
            if waiting_on_tasks(app, session.id, goal):
                continue
            goal.update(phase="recovering", waiting_tasks=[])
            _put_goal(app, session.id, goal)
            _enqueue_goal_redrive(
                app,
                session.id,
                GoalDecision(
                    outcome="redrive",
                    reason="goal_recovered",
                    new_state=goal,
                    guidance="The service restarted. Inspect retained task results and workspace state first. "
                    "Do not resubmit accepted operations or assume interrupted effects were undone. "
                    "Replan from the evidence before continuing.",
                ),
                goal,
            )
