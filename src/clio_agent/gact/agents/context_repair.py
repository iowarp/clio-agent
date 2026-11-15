"""Repair tool calls whose result was never recorded, from the stored context itself.

A step records its calls and their results as one write
(:meth:`~clio_agent.gact.agents.clio_react_record.StepRecorder.step_done`), so a call
with no result in a scope's live context is damage: a step write torn by a crash on a
plain store, a torn clio-core put or delete, or a context edit that deleted the result.
The fold refuses such a context (``unanswered_call``), and it would refuse it on every
later turn, restart or not.

Before a forward reads its scope, :func:`repair_unanswered_calls` gives each such call
the error result ``operation_outcome_uncertain``: the truth, that its outcome is unknown
and nothing was run again, naming any task the call had already started. It is recorded
at the call's own step and turn, so a rewind of that turn removes both together. Each
repair is reported (``context_repaired_unanswered_calls``) on the trace and in the
transcript, never silently.

This is the only repair. Malformed or unreadable records stay typed fold and read errors
(:mod:`~clio_agent.gact.agents.context_errors`); nothing here guesses at them. A scope
restored from the trace or transcript gets the same treatment in
:func:`~clio_agent.gact.agents.context_identity.restore_context`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any

__all__ = [
    "CONTEXT_REPAIRED",
    "OUTCOME_UNCERTAIN",
    "interrupted_observation",
    "repair_unanswered_calls",
]

logger = logging.getLogger(__name__)

#: Typed reason recorded when calls without a result were given one.
CONTEXT_REPAIRED = "context_repaired_unanswered_calls"
#: The ``error`` of the result a call without a recorded result is given.
OUTCOME_UNCERTAIN = "operation_outcome_uncertain"

_MESSAGE = (
    "This call's result was never saved: its step was cut off (a stop, a crash or a "
    "service restart) before the result was recorded, so its outcome is unknown. The "
    "call was not run again. Inspect task handles and effects before deciding whether "
    "to retry."
)


def interrupted_observation(call_id: str, started: Sequence[Any] = ()) -> dict[str, Any]:
    """The error result recorded for a call whose own result was lost.

    Args:
        call_id: The call's id.
        started: The tasks the call had already started (``started_tasks``), named so
            the agent can observe or wait on them instead of starting the work again.
    """
    return {
        "call_id": call_id,
        "text": json.dumps(
            {
                "error": OUTCOME_UNCERTAIN,
                "message": _MESSAGE,
                "resubmitted": False,
                **started_by_call(started),
            }
        ),
        "is_error": True,
    }


def started_by_call(tasks: Sequence[Any]) -> dict[str, Any]:
    """What a call had already started, as recorded (empty when nothing).

    The call's result was lost, not its work: naming the task lets the agent observe or
    wait on it instead of starting the same work again. It reports the record; the agent
    decides.
    """
    if not tasks:
        return {}
    rows = [
        {"handle": row.handle, "kind": row.kind, "label": row.label, "status": row.status}
        for row in tasks
    ]
    names = "; ".join(
        f"{row['label']} ({row['kind']} {row['handle']}, {row['status']})" for row in rows
    )
    return {
        "started_tasks": rows,
        "started_note": (
            f"Before its result was lost this call had already started: {names}. It was "
            "not started again. Observe, wait on or restart that task by handle rather "
            "than starting the same work a second time. A task the service resumed is "
            "running again: restarting it cancels the work it has in progress."
        ),
    }


def repair_unanswered_calls(arc: Any, session: str, scope: str, *, turn_id: str = "") -> list[str]:
    """Record an ``operation_outcome_uncertain`` result for each call left without one.

    Args:
        arc: The context plane (it must answer ``context_view`` and ``insert_segment``).
        session: The session id.
        scope: The run-keyed agent scope.
        turn_id: The current turn, used only for a call recorded without its own turn.

    Returns:
        The repaired call ids, in context order (empty when nothing was missing).

    Raises:
        ContextReadError: The scope could not be read.
        ContextWriteError: A result could not be recorded.
    """
    from clio_agent.arc.context_restore import unanswered_calls
    from clio_agent.gact import context
    from clio_agent.gact.agents.context_errors import ContextReadError, ContextWriteError

    try:
        segments = list(arc.context_view(session, scope).segments)
    except Exception as exc:  # noqa: BLE001 - re-raised typed, never swallowed
        raise ContextReadError(scope, exc) from exc
    if not _calls_identified(segments):
        return []  # a call without an id is malformed: the fold refuses it, typed
    missing = unanswered_calls(segments)
    insert = getattr(arc, "insert_segment", None)
    if not missing or insert is None:
        # A plane that cannot insert (the History mode) leaves the fold's typed refusal.
        return []
    from clio_agent.gact.agents.clio_react_record import _token_estimate
    from clio_agent.gact.spawn_invocation import started_tasks

    history = getattr(getattr(context.active_app(), "state", None), "task_history", None)
    repaired: list[str] = []
    texts: list[str] = []
    for offset, (position, call) in enumerate(missing):
        call_id = str(call.content["id"])
        started = started_tasks(history, session, call_id) if history is not None else []
        observation = interrupted_observation(call_id, started)
        write = partial(
            insert,
            session,
            scope,
            position + offset,
            "observation",
            observation,
            step=getattr(call, "step", -1),
            token_count=_token_estimate(observation),
            turn_id=str(getattr(call, "turn_id", "") or turn_id),
        )
        try:
            history.confirm_write(write) if history is not None else write()
        except Exception as exc:  # noqa: BLE001 - re-raised typed, never swallowed
            raise ContextWriteError(scope, "observation", exc) from exc
        repaired.append(call_id)
        texts.append(observation["text"])
    _report(session, scope, repaired, texts, turn_id)
    return repaired


def _calls_identified(segments: Sequence[Any]) -> bool:
    return all(
        isinstance(seg.content, Mapping) and seg.content.get("id")
        for seg in segments
        if getattr(seg, "kind", "") == "tool_call"
    )


def _report(session: str, scope: str, call_ids: list[str], texts: list[str], turn: str) -> None:
    """Put the repair on the log, the semantic trace and the live transcript."""
    from clio_agent.gact import context
    from clio_agent.gact.injection_parts import emit_injection
    from clio_agent.gact.runtime.globals import _emit_semantic_event

    logger.warning(
        "agent context repaired reason=%s session=%s scope=%s calls=%s",
        CONTEXT_REPAIRED,
        session,
        scope,
        ",".join(call_ids),
    )
    app = context.active_app()
    if app is None:
        return
    _emit_semantic_event(
        app,
        session,
        "context.repaired",
        turn_id=turn,
        status="repaired",
        summary=f"{len(call_ids)} tool call(s) without a saved result marked outcome unknown",
        payload={"reason": CONTEXT_REPAIRED, "scope": scope, "call_ids": call_ids},
    )
    for call_id, text in zip(call_ids, texts, strict=True):
        emit_injection("context_repair", f"Result recorded for call {call_id}: {text}")
