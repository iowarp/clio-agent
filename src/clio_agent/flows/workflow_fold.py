"""Declaration-order fold of a workflow's child ``workflow_state``.

A gather merges in call order because the call list is the program. A workflow's
steps finish in an order replay will not repeat, so the fold walks the definition
instead. The winner of a key is the later step in that definition. A conflict
event names the same handles either way the children finished, which is what
replay needs in order to record the event once.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STALL_STEP_TIMEOUT = "step_timeout"
STALL_TIMEOUT_REQUIRES_TASK = "timeout_requires_task"


def cancellation_is_deadline(
    cancellation_requested_at: str | None, execution_deadline: str | None
) -> bool:
    """A deadline cancel is the child ending at or after the deadline it was given.

    A person cancelling before that instant is a cancellation. The workflow ends
    ``cancelled`` for that, and offers a deadline to the editors as a failed step.
    """

    return bool(
        cancellation_requested_at
        and execution_deadline
        and cancellation_requested_at >= execution_deadline
    )


def fold_workflow_state(
    results: list[Any], indexes: list[int] | None = None
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """Fold ``workflow_state`` in the order given. ``indexes`` are definition positions.

    ``results`` must already be in definition order. The returned conflicts use
    each winner's definition index, so the row does not depend on which child
    finished first.
    """

    state: dict[str, Any] = {}
    sources: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for position, value in enumerate(results):
        sections = getattr(value, "workflow_state", None)
        if not isinstance(sections, Mapping) or not sections:
            continue
        handle = str(getattr(value, "handle", "") or "")
        index = indexes[position] if indexes is not None else position
        for key, section in sections.items():
            if key in state and state[key] != section:
                conflicts.append(
                    {
                        "key": str(key),
                        "earlier": sources.get(key, ""),
                        "later": handle,
                        "index": index,
                    }
                )
            state[key] = section
            sources[key] = handle
    return state, sources, conflicts


def deadline_cancelled(history: Any, handle: str) -> bool:
    """Whether this child was cancelled by the deadline stored on its assignment."""

    task = history.task_snapshot(handle)
    if task is None or not task.cancellation_requested_at or task.details is None:
        return False
    from clio_agent.arc.task_types import AssignmentDetails  # noqa: PLC0415

    details = AssignmentDetails.model_validate(history.read_content(task.details))
    return cancellation_is_deadline(task.cancellation_requested_at, details.execution_deadline)
