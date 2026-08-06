"""Shared task-status tally + message composer for polling tools' declared
``structured_content`` (P5 wire semantics, the ``wait_agent_tasks`` treatment
extended to ``check_agent_tasks`` / ``observe_agent_tasks``).

Both tools poll a set of this session's spawned children and need the SAME
"N tasks: X running, Y completed" summary line for the wire's declared
``message`` field (:func:`clio_agent.gact.agents.tool_instrumentation.declare_structured_content`).
This owner module holds the ONE tally/format so it is not duplicated between
``spawn_runtime.py`` (``check_agent_tasks``) and ``observe_runtime.py``
(``observe_agent_tasks``) — no-accretion (both call sites stay a single import
+ call, keeping their own ratcheted line counts flat).
"""

from __future__ import annotations

from collections.abc import Sequence

# Canonical status vocabulary in report order (mirrors AgentTask's own status
# catalog: agent_tasks.STATUS_RUNNING/QUEUED/COMPLETED/FAILED/CANCELLED). Any
# OTHER status-ish string a caller passes (e.g. "unknown_task", an invoker
# error reason) is still counted — appended after the known ones, never
# dropped (no-silent-fallback).
_STATUS_ORDER: tuple[str, ...] = ("running", "queued", "completed", "failed", "cancelled")
_STATUS_LABELS: dict[str, str] = {status: status for status in _STATUS_ORDER}


def task_status_counts(statuses: Sequence[str]) -> dict[str, int]:
    """Tally ``statuses`` into ``{status: count}`` (insertion order of first sight)."""

    counts: dict[str, int] = {}
    for status in statuses:
        key = str(status or "")
        counts[key] = counts.get(key, 0) + 1
    return counts


def task_status_message(statuses: Sequence[str]) -> str:
    """One-line honest summary: ``"N task(s): X running, Y completed"``.

    Known statuses report in :data:`_STATUS_ORDER`; unknown ones (e.g. an
    unresolved id's ``"unknown_task"`` row) are appended after, still counted.
    Empty input reports ``"no tasks"`` — never a bare ``"0 tasks: "``.
    """

    counts = task_status_counts(statuses)
    n = len(statuses)
    if n == 0:
        return "no tasks"
    parts = [f"{counts[key]} {_STATUS_LABELS[key]}" for key in _STATUS_ORDER if counts.get(key)]
    for key, count in counts.items():
        if key not in _STATUS_LABELS:
            parts.append(f"{count} {key}")
    return f"{n} task{'' if n == 1 else 's'}: " + ", ".join(parts)
