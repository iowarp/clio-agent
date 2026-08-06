"""Typed UI twin for consumed background-task exit notifications (#1131).

Also owns the task-terminal-transition writeback that closes a dangling
``delegate.started`` ``expert_handoff`` part on a parent's STORED message
(:func:`reconcile_stored_handoff_part`, round-9 wire defect) -- the natural
extension of this module's "a background task's terminal state must show up
on the parent's wire" charter, called from the SAME seam
(:func:`clio_agent.gact.task_fold.finish_agent_task_transition`) that already
fires :func:`emit_background_exit_part` for observe-later consumption.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from clio_agent.gact.agents.spawn_placement import run_handle_fields
from clio_agent.gact.events import Event
from clio_agent.gact.parts import Part
from clio_agent.gact.session_store import _replace_session_messages

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.agent_tasks import AgentTask

logger = logging.getLogger(__name__)

_EXIT_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "canceled",
}


def background_exit_part(task: "AgentTask") -> Part:
    """Build one additive ``background_exit`` part from a terminal task.

    Args:
        task: The terminal task whose observe-later notification won the shared
            consumption gate.

    Returns:
        A stable UI-facing part carrying the run handle, task/job identity, exit
        status, and an artifact reference only when the terminal fold supplied one.

    Raises:
        ValueError: If ``task`` is not in a terminal status.
    """

    exit_status = _EXIT_STATUS.get(task.status)
    if exit_status is None:
        raise ValueError(f"background exit requires a terminal task, got {task.status!r}")
    child_id = task.agent_ref.get("expert_id", "")
    parent_id = task.agent_ref.get("requesting_expert_id", "") or "main"
    fields = run_handle_fields(task, child_id)
    return Part(
        id=f"live_background_exit_{uuid.uuid4().hex[:12]}",
        type="background_exit",
        agent_id=child_id,
        parent_agent=parent_id,
        child_agent=child_id,
        handle_id=fields["handle_id"],
        run_label=fields["run_label"],
        live_state=fields["live_state"],
        host=fields["host"],
        placement=fields["placement"],
        task_id=task.task_id,
        job_id=task.task_id,
        exit_status=exit_status,
        artifact_ref=task.artifact_ref,
        status=task.status,
        metadata={"stream_source": "live"},
    )


def emit_background_exit_part(app: "FastAPI", session_id: str, task: "AgentTask") -> Part:
    """Append one typed exit part to the active parent turn and return it.

    This function owns only emission. Callers must first win
    :func:`agent_tasks.consume_notification`; invoking it for an unclaimed task
    would bypass the existing exactly-once gate.
    """

    from clio_agent.gact.tool_observer import _append_live_assistant_part  # noqa: PLC0415

    part = background_exit_part(task)
    _append_live_assistant_part(app, session_id, part)
    return part


def reconcile_stored_handoff_part(app: "FastAPI", task: "AgentTask") -> bool:
    """Close a dangling ``delegate.started`` handoff part at terminal-transition time.

    Round-9 wire defect: a parent turn that ends (idle, or the runaway circuit
    breaker) WITHOUT ever waiting on a spawned child leaves that child's
    ``delegate.started`` ``expert_handoff`` part on the PARENT's STORED message.
    The existing choreography only ever supersedes it with a ``delegate.completed``
    part when the parent gets ANOTHER turn (``enrichment.consume_pending_agent_task_notifications``
    at the next turn's commit-to-run seam) or a mid-turn inbox drain
    (``loop_inbox``) -- both require the parent to run again. An idle parent that
    never gets (or hasn't yet gotten) a next turn never triggers either path, so
    ``GET /messages`` renders "running" forever even though ``GET /agent-tasks``
    and the SSE ``agent.task.completed`` event already disagree (observed live,
    session ``sess_539d24da07bf``: 3 spawned children stranded ``running`` after
    ``main`` ended on the circuit breaker).

    Called for EVERY terminal task from the task-terminal-transition seam
    (:func:`clio_agent.gact.task_fold.finish_agent_task_transition`), independent
    of consumption/notification -- this is a pure wire-truth fix, not a
    narrative/grounding one, so it never touches ``notify_pending`` /
    ``delegation_reported`` and never races the observe-later choreography
    (:func:`emit_background_exit_part` / ``spawn_runtime._emit_delegation_terminal``):
    a parent turn still actively running keeps its started part on the LIVE
    ledger, not yet in ``app.state.messages`` -- this function only ever finds
    (and only ever touches) a part that has ALREADY been finalized into a stored
    message, which happens exactly when no live turn is around to update it.

    Idempotent (handle_id-keyed, mirrors :meth:`TurnTranscript.upsert_delegation_part`'s
    collapse rule -- same part id/sequence kept, terminal fields layered over the
    started ones): a part already carrying ``stage == "delegate.completed"`` is
    left untouched, so a retried fold or a duplicate transport observation never
    double-writes or double-publishes.

    Never raises: runs among terminal side effects on the completion-callback
    thread (the same discipline :func:`clio_agent.gact.delegation_return.stamp_delegation_return`
    documents) -- every failure is logged with a typed reason instead.

    Args:
        app: The GACT app (message store + event bus on ``app.state``).
        task: A terminal :class:`~clio_agent.gact.agent_tasks.AgentTask`.

    Returns:
        ``True`` when a stale started part was found and closed; ``False`` on
        any no-op (task not terminal, no matching part, already superseded, or
        a best-effort failure).
    """

    try:
        return _reconcile_stored_handoff(app, task)
    except Exception as exc:  # noqa: BLE001 - terminal effects must never crash the fold
        logger.warning(
            "expert_handoff stale-close failed reason=reconcile_error task=%s parent=%s err=%r",
            task.task_id,
            task.parent_session_id,
            exc,
        )
        return False


def _reconcile_stored_handoff(app: "FastAPI", task: "AgentTask") -> bool:
    if not task.is_terminal:
        return False
    parent_sid = str(task.parent_session_id or "")
    handle_id = str(task.handle_id or task.task_id or "")
    if not parent_sid or not handle_id:
        return False

    messages = app.state.messages.get(parent_sid, []) or []
    for message in messages:
        for index, part in enumerate(message.parts):
            if part.type != "expert_handoff" or str(part.handle_id or "") != handle_id:
                continue
            if part.stage != "delegate.started":
                # Already superseded (a live wait/drain got there first) or not
                # the started marker -- never rewritten, never double-published.
                return False
            terminal_part = _stored_terminal_handoff_part(app, task)
            # Same collapse rule as TurnTranscript.upsert_delegation_part: keep
            # the started part's identity, layer the terminal metadata on top of
            # (never replacing) whatever the started row carried (e.g. "question").
            terminal_part.id = part.id
            terminal_part.sequence = part.sequence
            terminal_part.metadata = {**part.metadata, **terminal_part.metadata}
            message.parts[index] = terminal_part
            _replace_session_messages(app, parent_sid, list(messages))
            app.state.bus.publish(
                Event(
                    type="message.part.updated",
                    session_id=parent_sid,
                    payload={
                        "turn_id": message.turn_id,
                        "message_id": message.id,
                        "stream_source": str(terminal_part.metadata.get("stream_source") or "live"),
                        "part": terminal_part.to_wire(),
                    },
                )
            )
            return True
    return False


def _stored_terminal_handoff_part(app: "FastAPI", task: "AgentTask") -> Part:
    """Build the terminal Part with the SAME grammar ``_return_handoff_part`` uses."""

    from clio_agent.gact.agents.spawn_runtime import (  # noqa: PLC0415
        _completion_payload,
        _return_handoff_part,
    )
    from clio_agent.gact.types import AgentDef  # noqa: PLC0415

    parent_id = task.agent_ref.get("requesting_expert_id", "") or "main"
    agent_def = AgentDef(id=parent_id, title=parent_id)
    payload = _completion_payload(app, task)
    return _return_handoff_part(agent_def, task, payload)
