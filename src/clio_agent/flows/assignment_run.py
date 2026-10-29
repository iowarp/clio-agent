"""Which run a turn of an agent's own flow belongs to.

An agent whose selected flow is not ``react`` runs that flow for each written request
(a person's message, a promoted queued message, a goal, a schedule...): the run is
keyed by that input and the flow's ``task`` is its text. A conversation is also woken
by things that are not requests -- a child's result, the restart's recovery notice,
a delegation -- and admission gives such a turn the generic continuation text
(``Conversations._admit``). That text is not the flow's input: a run started on it
works on the wrong request and orphans the children of the run it interrupted.

So a turn woken by anything but a written request continues its assignment's run,
decided from durable state only (the trigger's mail kind, the assignment's workflow
checkpoints and its opening input), never from text:

1. the latest run this flow recorded under the assignment, on its recorded request
   (the journal re-attaches every accepted call and recorded step);
2. with nothing recorded yet, the run the assignment's opening input started: a
   written request (on its own text) or a delegation (on the delegated assignment);
3. an assignment opened by a wake after an earlier assignment's run ended does not
   run the flow again (:class:`AssignmentRun` with ``ended``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clio_agent.gact.session_mail import WRITTEN_INPUT_KINDS


@dataclass(frozen=True)
class AssignmentRun:
    """The run a woken turn continues, or the earlier assignment whose run ended."""

    invocation: str = ""  #: The run invocation (``flow:<id>:<marker>``) to continue.
    request: Any = None  #: The run's request: the flow's ``task``.
    ended: str = ""  #: Set instead: the assignment whose run already ended.


def written_text(history: Any, item: Any) -> str | None:
    """The text a written-request mail item gives its turn (as admission reads it)."""

    value = history.read_content(item.content)
    if isinstance(value, dict):
        text = value.get("text", "")
        return text if isinstance(text, str) else None
    return value if isinstance(value, str) and item.kind in {"human", "composer"} else None


def assignment_run(
    history: Any, flow_id: str, root_handle: str, session_id: str, trigger_id: str
) -> AssignmentRun | None:
    """The run a turn of ``flow_id`` in assignment ``root_handle`` continues.

    ``None`` when the turn's trigger is a written request (it starts its own run) or
    nothing durable names a run; the caller then keys the run by the trigger.
    """

    if history is None or not root_handle or not trigger_id or flow_id == "react":
        return None
    trigger = history.mail_item(session_id, trigger_id)
    if trigger is None or trigger.kind in WRITTEN_INPUT_KINDS:
        return None
    prefix = f"flow:{flow_id}:"
    checkpoint = history.workflow_checkpoint(root_handle)
    if checkpoint is not None and checkpoint.invocation_id.startswith(prefix):
        content = history.read_content(checkpoint.content)
        if isinstance(content, dict) and "request" in content:
            return AssignmentRun(checkpoint.invocation_id, content["request"])
    assignment = history.task_snapshot(root_handle)
    if assignment is None:
        return None
    opening = history.mail_item(session_id, assignment.invocation_id)
    if opening is not None and opening.kind in WRITTEN_INPUT_KINDS:
        text = written_text(history, opening)
        if text is not None:
            return AssignmentRun(prefix + opening.id, text)
    delegation = history.mail_item(session_id, "assignment:" + root_handle)
    if delegation is not None and delegation.kind == "delegation":
        value = history.read_content(delegation.content)
        if isinstance(value, dict) and isinstance(value.get("assignment"), str):
            return AssignmentRun(prefix + delegation.id, value["assignment"])
    predecessor = _predecessor(history, assignment)
    return AssignmentRun(ended=predecessor) if predecessor else None


def _predecessor(history: Any, assignment: Any) -> str:
    from clio_agent.arc.task_types import AssignmentDetails  # noqa: PLC0415

    if assignment.details is None:
        return ""
    details = AssignmentDetails.model_validate(history.read_content(assignment.details))
    return str(details.predecessor or "")


__all__ = ["AssignmentRun", "assignment_run", "written_text"]
