"""Fan-out group identity + ``wait_agent_tasks`` wire presentation (P5).

Split out of ``spawn_runtime.py`` to hold the size ratchet (#714/#774 — "no
accretion": fixes that add more than a trivial amount of code go in an owner
module, not appended to a god file). Two small, pure concerns live here:

* :func:`spawn_group_fields` — the ``spawn_group_id``/``group_size`` metadata
  pair an ``expert_handoff`` Part stamps when its run belongs to a
  ``spawn_agents_parallel`` batch (absent for a single ``spawn_agent_task``
  spawn or a declared ``run_workflow`` step).
* :func:`wait_structured_row` / :func:`wait_summary` — the declared, typed
  structured shape ``wait_agent_tasks`` attaches to its wire
  ``structured_content`` (owner ruling: presentation via a DECLARED shape,
  never inferred JSON key order — the same contract an MCP tool's
  ``outputSchema`` gives).

Both callers (``spawn_runtime.py``) own the CALL SITES and the actual
tool/Part construction; this module owns only the pure derivation logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def failed_spawn_metadata_row(
    child_id: str, parent_id: str, reason: str, spawn_group_id: str, group_size: int
) -> dict[str, Any]:
    """Terminal ``expert_handoff`` metadata row for a batch sibling that never
    spawned (P5 review finding [E]): a refused spawn inside a
    ``spawn_agents_parallel`` batch must still occupy its slot on the SAME
    ``spawn_group_id``/``group_size`` -- otherwise a 3-wide fanout with one
    refusal leaves ``group_size`` 3 with only 2 parts, unreconcilable forever.
    Concludes directly on the terminal lane (#882: success/failure share one
    lane); ``reason`` is the typed ``SpawnError.reason`` (e.g. "undeclared_child"),
    matching the SAME "error" key convention ``_return_handoff_part`` stamps a
    failed task's typed ``error_reason`` under -- never a raw exception message.
    """

    return {
        "agent_id": child_id,
        "parent_id": parent_id,
        "status": "failed",
        "stage": "delegate.completed",
        "error": reason,
        "spawn_group_id": spawn_group_id,
        "group_size": group_size,
    }


def spawn_group_fields(run: Any) -> dict[str, Any]:
    """The fan-out GROUP identity fields for an ``expert_handoff`` Part's metadata
    row: ``spawn_group_id`` + ``group_size`` when ``run`` (a
    :class:`~clio_agent.gact.agents.invoker.TaskHandle` /
    :class:`~clio_agent.gact.agents.invoker.TaskResult` /
    :class:`~clio_agent.gact.agent_tasks.AgentTask`) carries a non-empty
    ``spawn_group_id`` — the id ``spawn_agents_parallel`` mints ONCE per call
    and stamps on every sibling it spawns. Absent (not present as empty/null)
    for a single ``spawn_agent_task`` spawn or a declared ``run_workflow``
    step, so the UI groups Call boxes by explicit server identity, never by
    adjacency/timing.
    """

    group_id = str(getattr(run, "spawn_group_id", "") or "")
    if not group_id:
        return {}
    return {"spawn_group_id": group_id, "group_size": int(getattr(run, "group_size", 0) or 0)}


def wait_structured_row(
    name: str,
    status: str,
    duration_ms: float,
    answer_excerpt: str,
    waited_ms: float = 0.0,
) -> dict[str, Any]:
    """One row of ``wait_agent_tasks``'s declared structured ``results`` — the
    UI-ladder-friendly per-task summary: display ``name``
    (:func:`clio_agent.gact.agent_tasks.display_run_name`), typed ``status``,
    child wall-clock ``duration_ms``, time elapsed within this wait as
    ``waited_ms``, and the ALREADY-BOUNDED ``answer_excerpt``
    (never the full verbatim output — that stays on the model-facing
    ``results`` row, the #880 fidelity contract). Used for both a resolved
    task and an unknown-id/error row (``name`` falls back to the raw id,
    ``duration_ms`` 0.0, ``answer_excerpt`` empty).
    """

    return {
        "name": name,
        "status": status,
        "duration_ms": duration_ms,
        "waited_ms": waited_ms,
        "answer_excerpt": answer_excerpt,
    }


def wait_completion_offset_ms(wait_started_at: datetime, task_updated_at: str) -> float:
    """Return when a child completed relative to this wait's wall-clock start."""

    try:
        completed_at = datetime.fromisoformat(task_updated_at)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=wait_started_at.tzinfo)
        offset_ms = (completed_at - wait_started_at).total_seconds() * 1000
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, offset_ms)


# Typed status vocabulary the wait summary tallies, in the order it reports
# them — a literal count over AgentTask's own status catalog (+ the
# unknown_task/error-reason rows this tool can also return), never a
# prose/keyword guess.
_WAIT_SUMMARY_ORDER = ("completed", "failed", "cancelled", "running", "queued")
_WAIT_SUMMARY_LABELS = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "running": "still running",
    "queued": "still queued",
}


def wait_summary(_elapsed_s: float, rows: list[dict[str, Any]]) -> str:
    """The one-line human summary the wire's structured_content ladder shows
    FIRST (owner ruling: presentation via a DECLARED shape, never inferred
    dict-key order), e.g. ``"3 tasks: 1 completed, 2 still running"``.
    The shared activity row already shows the call duration, so repeating the
    elapsed time here makes the transcript harder to scan. This is a literal
    tally over each row's typed ``status``; any status
    outside the known vocabulary (e.g. ``unknown_task``, an invoker error
    reason) is still counted, appended after the known ones.
    """

    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("status") or "")
        counts[key] = counts.get(key, 0) + 1
    parts = [
        f"{counts[key]} {_WAIT_SUMMARY_LABELS[key]}"
        for key in _WAIT_SUMMARY_ORDER
        if counts.get(key)
    ]
    for key, count in counts.items():
        if key not in _WAIT_SUMMARY_LABELS:
            parts.append(f"{count} {key}")
    breakdown = ", ".join(parts) if parts else "no tasks"
    n = len(rows)
    return f"{n} task{'' if n == 1 else 's'}: {breakdown}"
