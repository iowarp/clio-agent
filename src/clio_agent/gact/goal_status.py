"""Read-only native goal status projection."""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.goal import _budget_spent, _get_goal


def _goal_status_result(result: dict[str, Any]) -> dict[str, Any]:
    """Declare ``goal_status``'s typed wire payload (P5 wire semantics — the
    ``wait_agent_tasks`` treatment) then return ``result`` unchanged (the
    model-facing shape is untouched). ``message`` is derived from the SAME
    armed-state facts, never a readback the model could steer toward."""

    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
    )

    if result.get("active"):
        message = (
            f"goal active: {result.get('condition', '')!r} "
            f"({result.get('iters_elapsed', 0)}/{result.get('max_goal_iters', 0)} iters)"
        )
    else:
        message = "no active goal"
    declare_structured_content({"message": message, **result})
    return result


# goal_status is read-only; the setter and event-driven wait are separate tools.
def build_goal_status_tool() -> Any:
    """Build the read-only goal inspector; completion belongs to the judge."""

    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def goal_status() -> dict[str, Any]:
        """Read this session's active goal condition and progress (read-only).

        Returns ``{active, condition, iters_elapsed, max_goal_iters, budget_spent}``. This is
        armed state only: it runs no evaluation, does not settle the goal, and does not end the turn. You
        can use set_goal to start an objective and wait_for to await owned tasks.
        Completion belongs to the independent judge; explicit user stops are authoritative."""

        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        if app is None or not sid:
            return _goal_status_result({"active": False, "condition": "", "iters_elapsed": 0})
        goal = _get_goal(app, sid)
        if not goal or not goal.get("active"):
            return _goal_status_result({"active": False, "condition": "", "iters_elapsed": 0})
        elapsed_s, tokens_spent = _budget_spent(app, sid, goal)
        return _goal_status_result(
            {
                "active": True,
                "condition": str(goal.get("condition") or ""),
                "iters_elapsed": int(goal.get("iters_elapsed", 0) or 0),
                "max_goal_iters": int(goal.get("max_goal_iters", 0) or 0),
                "phase": str(goal.get("phase") or "working"),
                "waiting_tasks": list(goal.get("waiting_tasks") or []),
                "budget_spent": {"wallclock_s": elapsed_s, "tokens": tokens_spent},
            }
        )

    return native_tool(
        goal_status,
        name="goal_status",
        presentation="goal",
        domain="goals",
        desc=goal_status.__doc__,
        title="Goal Status",
        args={},
        read_only=True,
    )
