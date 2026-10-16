"""Retained, observable goal-check activity, without blocking the server loop."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def begin_goal_check(app: Any, sid: str, goal: dict[str, Any], turn_id: str, trace_id: str) -> bool:
    """Publish the checking phase only while this exact objective still owns the session."""
    from clio_agent.gact.goal import _GOAL_MUTEX, _get_goal, _put_goal
    from clio_agent.gact.runtime.globals import _emit_semantic_event

    with _GOAL_MUTEX:
        current = _get_goal(app, sid)
        if (
            not current.get("active")
            or current.get("goal_id") != goal.get("goal_id")
            or current.get("phase") == "judging"
            or (turn_id and current.get("last_evaluated_turn_id") == turn_id)
        ):
            return False
        current.update(
            phase="judging",
            judge_started_at=datetime.now(timezone.utc).isoformat(),
            judge_turn_id=turn_id,
        )
        _put_goal(app, sid, current)
        goal.update(current)
    try:
        _emit_semantic_event(
            app,
            sid,
            "goal.judging",
            turn_id=turn_id,
            trace_id=trace_id,
            status="running",
            summary="Checking goal completion",
            actor={"component": "goal"},
            payload={"goal_id": goal["goal_id"]},
        )
    except (OSError, RuntimeError):
        logger.warning(
            "reason=goal_activity_delivery_failed retained checking phase remains available",
            exc_info=True,
        )
    return True


def cancel_goal_check(app: Any, sid: str, goal_id: str) -> None:
    """Retain an interrupted check as paused; never leave a phantom checking state."""
    from clio_agent.gact.goal import _GOAL_MUTEX, _get_goal, _put_goal

    with _GOAL_MUTEX:
        current = _get_goal(app, sid)
        if current.get("goal_id") == goal_id and current.get("phase") == "judging":
            current.update(
                active=False, paused=True, phase="paused", clear_reason="goal_judge_interrupted"
            )
            _put_goal(app, sid, current)
