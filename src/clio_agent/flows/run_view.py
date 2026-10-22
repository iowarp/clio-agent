"""What a flow run shows: its checkpoint in task history, projected for the run view.

A flow run is the checkpoint under its owning assignment (D1). The projection
joins the flow's published view (the ``workflow`` flow publishes its steps, and the
proposals awaiting approval) with the run's recorded calls, so each step carries
its task handle and that task's status, and lists the run's events and open
questions. A run that ended on a typed outcome carries it as ``outcome``.
"""

from __future__ import annotations

from typing import Any

#: Call statuses after which the call no longer waits on anything.
_SETTLED = frozenset({"completed", "failed", "cancelled", "task_state_uncertain"})


def flow_run(history: Any, handle: str) -> dict[str, Any] | None:
    """The latest flow run checkpointed under assignment ``handle``, or ``None``."""

    checkpoint = history.workflow_checkpoint(handle)
    if checkpoint is None:
        return None
    content = history.read_content(checkpoint.content)
    if not isinstance(content, dict) or "flow" not in content:
        return None
    calls = dict(content.get("calls") or {})
    view = content.get("view")
    if isinstance(view, dict) and isinstance(view.get("steps"), list):
        view = {**view, "steps": [_step(row, calls) for row in view["steps"]]}
    status = str(content.get("status") or "running")
    result = content.get("result")
    return {
        "run_id": content.get("run_id", ""),
        "flow": content["flow"],
        "status": status,
        "depth": int(content.get("depth") or 0),
        "root_handle": content.get("root_handle", handle),
        "entries": int(content.get("entries") or 0),
        "view": view,
        "calls": [
            {
                "invocation": invocation,
                "kind": row.get("kind", ""),
                "target": row.get("target", ""),
                "handle": row.get("handle", ""),
                "status": row.get("status", ""),
            }
            for invocation, row in calls.items()
        ],
        "questions": [
            {"handle": row.get("handle", ""), "question": row.get("target", "")}
            for row in calls.values()
            if row.get("kind") == "ask" and row.get("status") not in _SETTLED
        ],
        "events": list((content.get("events") or {}).values()),
        "outcome": result if status not in {"running", "completed"} else None,
        "result": result if status == "completed" else None,
    }


def _step(row: dict[str, Any], calls: dict[str, dict[str, Any]]) -> dict[str, Any]:
    call = calls.get(str(row.get("invocation") or "")) or {}
    return {
        **row,
        "handle": row.get("handle") or call.get("handle", ""),
        "task_status": call.get("status", ""),
    }
