"""Event-driven goal waiting on the existing, owned task mailbox."""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context as ctx
from clio_agent.gact.goal import _GOAL_MUTEX, GoalError, _get_goal, _put_goal
from clio_agent.gact.task_projection import TERMINAL, resolve_task


def waiting_on_tasks(app: Any, sid: str, goal: dict[str, Any]) -> bool:
    """Check retained handles without collecting results or launching another operation.

    Missing or inaccessible tasks resume the agent to diagnose the failure. Terminal
    failures and cancellations also resume it; they never imply the goal is complete.
    """
    handles = goal.get("waiting_tasks") or []
    if not handles:
        return False
    settled = []
    for handle in handles:
        try:
            row = resolve_task(app, sid, handle)
            settled.append(row["effective_status"] in TERMINAL)
        except ValueError:
            return False
    ready = any(settled) if goal.get("wait_return_when") == "any" else all(settled)
    return not ready


def wait_for(tasks: str | list[str], return_when: str = "all") -> dict[str, Any]:
    """Park the active goal until any/all specified tasks settle, then finish this turn.

    Task completion wakes the conversation through its normal results mailbox. This
    does not poll, collect results, cancel tasks, or declare the goal complete. Use
    wait_tasks instead to remain inside the current turn until results arrive.
    """
    app, sid = ctx.active_app(), ctx.active_session_id()
    if return_when not in {"any", "all"}:
        raise ValueError("return_when must be any or all")
    handles = [tasks] if isinstance(tasks, str) else tasks
    if not handles:
        raise ValueError("At least one task handle is required")
    rows = [resolve_task(app, sid, handle) for handle in handles]
    if any(row["owner"]["session_id"] != sid for row in rows):
        raise ValueError("Goal waits require tasks owned by this conversation")
    with _GOAL_MUTEX:
        goal = _get_goal(app, sid)
        if not goal.get("active"):
            raise GoalError("Start a goal before parking it", reason="goal_not_active")
        goal.update(
            waiting_tasks=list(dict.fromkeys(row["handle"] for row in rows)),
            wait_return_when=return_when,
            output_repeats=0,
        )
        waiting = waiting_on_tasks(app, sid, goal)
        goal["phase"] = "waiting" if waiting else "working"
        _put_goal(app, sid, goal)
    return {
        "waiting": waiting,
        "tasks": goal["waiting_tasks"],
        "return_when": return_when,
        "message": "Finish this turn; task completion will resume the goal."
        if waiting
        else "Tasks have settled; inspect their results and continue the goal.",
    }


def build_goal_wait_tool() -> Any:
    """Build the nonblocking, event-driven wait tool for an active goal."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    return native_tool(
        wait_for,
        name="wait_for",
        desc=wait_for.__doc__,
        domain="autonomy",
        title="Wait for tasks",
        presentation="goal_wait",
        args={
            "tasks": {
                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]
            },
            "return_when": {"type": "string", "enum": ["any", "all"]},
        },
    )
