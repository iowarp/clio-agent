"""Whether a conversation assignment is waiting for input that nothing in CLIO will send.

A subagent can end its turn with ``assignment_disposition="yield"``: it keeps its
assignment open for a follow-up message instead of completing it. A react parent sees
such a child as pending and can message it. A flow waits for a terminal result, so a
child that yields with nothing left to wake it held the flow forever and nobody was
told (live: a workflow's researcher step yielded after its fetches were refused, and
the workflow never moved). A flow's wait asks :func:`waiting_for_input` and settles
such a child as :data:`AWAITING_INPUT` with its last answer: the flow decides what to
do with it, and the child stays open for a person to resume.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from clio_agent.arc.task_types import AssignmentDetails, TaskSnapshot

AWAITING_INPUT: Final = "awaiting_input"

Waiting = Literal["awaiting_input", "retiring", ""]


def waiting_for_input(app: Any, task: TaskSnapshot) -> Waiting:
    """``awaiting_input`` when ``task`` yielded and nothing in CLIO will wake it.

    ``retiring`` when it yielded but its turn has not released yet (look again once it
    has); ``""`` for anything else: running, finished, or yielded on work that will wake
    it (its own child tasks, its dependencies, an execution gate, or deliverable input).
    """

    if (
        task.kind not in {"Session", "Subagent"}
        or task.terminal_at is not None
        or task.activity != "yielded"
    ):
        return ""
    history, conversations = app.state.task_history, app.state.conversations
    sid = task.owner.session_id
    if conversations.runner.busy(sid):
        return "retiring"
    session = app.state.sessions.get(sid)
    if session is not None and session.metadata.get("execution_gate"):
        return ""
    with history.serialized():
        if any(handle != task.handle for handle in history.tree_handles(sid, active_only=True)):
            return ""
        details = _details(history, task)
        for handle in details.dependencies:
            dependency = history.task_snapshot(handle)
            if dependency is None or dependency.terminal_at is None:
                return ""
        if any(
            item.delivery == "eligible" and conversations.eligible(sid, item)
            for item in history.pending_mail(sid)
        ):
            return ""
    return AWAITING_INPUT


def awaiting_input_outcome(app: Any, task: TaskSnapshot) -> dict[str, Any]:
    """The wait outcome for a child waiting for input: its status and last answer."""

    history = app.state.task_history
    with history.serialized():
        details = _details(history, task)
        last = history.read_content(details.last_output) if details.last_output else None
    return {
        "handle": task.handle,
        "status": AWAITING_INPUT,
        "reason": "the child ended its turn waiting for input that nothing will send",
        "result": last,
    }


def _details(history: Any, task: TaskSnapshot) -> AssignmentDetails:
    return (
        AssignmentDetails.model_validate(history.read_content(task.details))
        if task.details
        else AssignmentDetails()
    )
