"""What a flow run shows: its checkpoint in task history, projected for the run view.

A flow run is the checkpoint under its owning assignment (D1). The projection
joins the flow's published view (the ``workflow`` flow publishes its steps, and the
proposals awaiting approval) with the run's recorded calls, so each step carries
its task handle and that task's status, and lists the run's events and open
questions. A run that ended on a typed outcome carries it as ``outcome``. A step's
forks (``gact/agent_fork.py``) are read from task history and drawn beside it as
``<step>~<n>`` while they run, before the step's wait reports them to the flow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from clio_agent.flows.execution_view import execution_view

#: Call statuses after which the call no longer waits on anything.
_SETTLED = frozenset({"completed", "failed", "cancelled", "task_state_uncertain"})


def flow_run(
    history: Any, handle: str, *, reconcile_children: bool = False
) -> dict[str, Any] | None:
    """The latest flow run checkpointed under assignment ``handle``, or ``None``."""

    checkpoint = history.workflow_checkpoint(handle)
    if checkpoint is None:
        return None
    content = history.read_content(checkpoint.content)
    if not isinstance(content, dict) or "flow" not in content:
        return None
    calls = dict(content.get("calls") or {})
    if reconcile_children:
        calls = _confirmed_children(history, handle, content, calls)
    view = content.get("view")
    if isinstance(view, dict) and isinstance(view.get("steps"), list):
        steps = [_step(row, calls) for row in view["steps"]]
        view = {**view, "steps": [row for step in steps for row in (step, *_forks(history, step))]}
    status = str(content.get("status") or "running")
    result = content.get("result")
    projected = {
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
                **({"recovered": True} if row.get("recovered") else {}),
                **({"returned_after_stop": True} if row.get("returned_after_stop") else {}),
                # Calls started together (one ``gather``) share the first one's invocation.
                **({"together": row["together"]} if row.get("together") else {}),
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
    if content["flow"] not in {"workflow", "council", "arena"}:
        projected["execution"] = execution_view(content, projected["calls"])
    return projected


def _confirmed_children(
    history: Any, handle: str, content: dict[str, Any], calls: dict[str, Any]
) -> dict[str, Any]:
    """Reconcile only direct children attributed to this exact flow invocation.

    The route authorizes the owning task before this projection. Nothing is
    written back to history, and neither session siblings nor restarts are joined.
    """
    run_id = str(content.get("run_id") or "")
    root = history.task_snapshot(handle)
    if not run_id or root is None:
        return calls
    result = dict(calls)
    children = [
        history.task_snapshot(child_handle) for child_handle in history.descendant_handles(handle)
    ]
    for child in sorted(
        (row for row in children if row is not None), key=lambda row: (row.created_at, row.handle)
    ):
        if (
            child is None
            or child.owner.parent_handle != handle
            or child.owner.human_id != root.owner.human_id
            or not child.invocation_id.startswith(run_id + ":")
            or child.invocation_id in result
        ):
            continue
        result[child.invocation_id] = {
            "kind": child.invocation_id[len(run_id) + 1 :].partition(":")[0],
            "target": child.label,
            "handle": child.handle,
            "status": child.status,
            "recovered": True,
            "returned_after_stop": (
                child.status == "completed"
                and root.terminal_at is not None
                and child.terminal_at is not None
                and datetime.fromisoformat(child.terminal_at)
                > datetime.fromisoformat(root.terminal_at)
            ),
        }
    return result


#: A fork task's status, as the step status the railroad draws.
_FORK_STATUS = {"completed": "completed", "failed": "failed", "cancelled": "cancelled"}


def _forks(history: Any, step: dict[str, Any]) -> list[dict[str, Any]]:
    """The step's forks, labelled ``<step>~<n>`` in fork order, beside the step."""

    from clio_agent.gact.task_forks import fork_family  # noqa: PLC0415

    handle = str(step.get("handle") or "")
    if not handle or "~" in str(step.get("id") or ""):
        return []
    rows = []
    for number, member in enumerate(fork_family(history, handle)[1:], 1):
        task = history.task_snapshot(member)
        if task is None:
            continue
        rows.append(
            {
                "id": f"{step.get('id')}~{number}",
                "target": step.get("target", ""),
                "after": list(step.get("after") or []),
                "status": _FORK_STATUS.get(task.status, "running"),
                "handle": task.handle,
                "task_status": task.status,
                "fork_of": step.get("id"),
                "note": task.description or "",
            }
        )
    return rows


def _step(row: dict[str, Any], calls: dict[str, dict[str, Any]]) -> dict[str, Any]:
    call = calls.get(str(row.get("invocation") or "")) or {}
    return {
        **row,
        "handle": row.get("handle") or call.get("handle", ""),
        "task_status": call.get("status", ""),
    }
