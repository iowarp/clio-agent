"""A flow outcome is separate from completion of its owning conversation task."""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.arc.task_types import AssignmentDetails
from clio_agent.errors import ClioError
from clio_agent.flows.run_view import flow_run

logger = logging.getLogger(__name__)


def task_flow_summary(history: Any, task: dict[str, Any], flow_id: str) -> dict[str, Any]:
    """Read this assignment's checkpoint; never inherit a predecessor's success.

    A started-flow assignment after an ended run forwards later input without
    running the flow again, unless its recorded invocation is an explicit restart.
    Its predecessor is a link to earlier work, not this assignment's outcome.
    """
    summary: dict[str, Any] = {"id": flow_id, "status": "unknown"}
    try:
        run = flow_run(history, task["handle"])
        if run is not None:
            outcome = run["outcome"]
            detail = (
                {
                    "reason": str(outcome.get("reason") or ""),
                    "message": str(outcome.get("message") or ""),
                }
                if isinstance(outcome, dict)
                else {"reason": str(run["status"]), "message": str(outcome)}
                if outcome is not None
                else None
            )
            return {**summary, "status": run["status"], "outcome": detail}
        snapshot = history.task_snapshot(task["handle"])
        if task["status"] == "completed" and snapshot is not None and snapshot.details is not None:
            details = AssignmentDetails.model_validate(history.read_content(snapshot.details))
            if details.predecessor and not str(task.get("invocation_id") or "").startswith(
                "restart:"
            ):
                return {**summary, "status": "not_run", "previous_handle": details.predecessor}
    except (ValueError, ClioError) as error:
        logger.warning("Flow outcome unavailable handle=%s reason=%s", task["handle"], error)
        summary["detail"] = "The recorded flow outcome could not be read."
    return summary
