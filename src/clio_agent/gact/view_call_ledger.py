"""What the task ledger keeps of a view's own call, and how one that was cut off ends.

A call a view makes (``tools/call`` from an MCP App, its resource reads, a reopen's
replay) is the view's, not the agent's. The agent never waits for it and learns what the
view did from the view's own context. Two things follow, both recorded here:

* **Origin (MC-F2).** Its submission is attributed ``origin: "view"`` (:func:`attribution`),
  the same mark the server receives in ``_meta`` (:mod:`clio_agent.tools.call_origin`).
* **A cut-off or failed call ends terminal, settlement unknown (MC-F2).** The PDF viewer
  always has a 30 s long-poll in flight. Closing its page or restarting CLIO cancelled
  that poll, and the ledger recorded ``task_state_uncertain`` with an attention episode,
  which woke the agent for an unprompted turn of 70-108K tokens about a call it never
  made. Such a call now ends ``cancelled`` (``failed`` when its submission failed) with
  ``settlement: "unknown"`` and the typed reason in its ``raw_status`` and progress
  (:func:`settle_cut_off`), with a log line, and opens no attention episode, so no model
  is woken. An agent's call keeps its path unchanged.

What such a call's record holds (its arguments and reply) is compacted by
:mod:`clio_agent.tools.view_only_results` when the model cannot see the tool (MC-F3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import JsonValue

from clio_agent.arc.task_types import SubmissionContext, TaskProgress, TaskStatus
from clio_agent.gact.native_work import now
from clio_agent.tools import call_origin

if TYPE_CHECKING:
    from clio_agent.gact.external_tasks import ExternalTasks
    from clio_agent.gact.task_history import TaskHistory

logger = logging.getLogger(__name__)

#: ``attribution.origin`` of a view's call in its submission record.
VIEW_ORIGIN = "view"
#: Typed reason when CLIO restarted while a view's call was in flight.
VIEW_CALL_INTERRUPTED = "mcp_app_call_interrupted"
#: Typed reason when a view's submission failed with its acceptance unknown.
VIEW_CALL_FAILED = "mcp_app_call_failed"
VIEW_CALL_INTERRUPTED_TEXT = (
    f"{VIEW_CALL_INTERRUPTED}: CLIO stopped while the view's call was in flight; nothing "
    "was resubmitted and whether the service acted on it is not known."
)


def uncertain(
    tasks: ExternalTasks, submission: SubmissionContext, handle: str, reason: str
) -> Callable[[], None]:
    """The record of a submission whose outcome is unknown.

    The agent's call opens an attention episode (``ExternalTasks._attention``), so the
    agent is told. A view's call ends ``failed`` with ``settlement: unknown`` and opens
    none: the view already received the failure, and the agent never made the call.
    """

    if not is_view_call(submission):
        return partial(tasks._attention, handle, reason)
    message = f"{VIEW_CALL_FAILED}: {reason}"
    return partial(settle_cut_off, tasks.history, handle, message, status="failed")


def attribution() -> dict[str, JsonValue]:
    """The origin mark for the submission being recorded (empty for the agent's call)."""

    return {"origin": VIEW_ORIGIN} if call_origin.current() == VIEW_ORIGIN else {}


def is_view_call(submission: SubmissionContext) -> bool:
    """Whether ``submission`` was a view's own call."""

    return submission.attribution.get("origin") == VIEW_ORIGIN


def settle_cut_off(
    history: TaskHistory, handle: str, message: str, *, status: TaskStatus = "cancelled"
) -> None:
    """End a view's cut-off call: terminal, ``settlement: unknown``, no attention episode.

    ``message`` starts with the typed reason (``mcp_app_call_cancelled`` when the view's
    request went away, :data:`VIEW_CALL_INTERRUPTED` after a restart,
    :data:`VIEW_CALL_FAILED` when the submission failed). A call that already ended keeps
    its outcome.
    """

    reason = message.split(":", 1)[0]
    with history.serialized():
        current = history.task_snapshot(handle)
        if current is None or current.terminal_at is not None:
            return
        stamp = now()
        update: dict[str, Any] = {
            "status": status,
            "raw_status": reason,
            "activity": "",
            "settlement": "unknown",
            "terminal_at": stamp,
            "updated_at": stamp,
            "progress": TaskProgress(message=message),
            "supported_actions": (),
        }
        history.record_snapshot(current.model_copy(update=update), observation_id=uuid4().hex)
    logger.info(
        "%s handle=%s tool=%s settlement=unknown attention=none", reason, handle, current.tool
    )


async def restored_cut_off(tasks: ExternalTasks, handle: str) -> None:
    """At start, end a view's call that was in flight when CLIO stopped (no wake)."""

    await tasks._record_or_defer(
        "external-recovery:" + handle,
        partial(settle_cut_off, tasks.history, handle, VIEW_CALL_INTERRUPTED_TEXT),
    )


__all__ = [
    "VIEW_CALL_FAILED",
    "VIEW_CALL_INTERRUPTED",
    "VIEW_CALL_INTERRUPTED_TEXT",
    "VIEW_ORIGIN",
    "attribution",
    "is_view_call",
    "restored_cut_off",
    "settle_cut_off",
    "uncertain",
]
