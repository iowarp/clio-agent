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
from collections import deque
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.spawn_placement import run_handle_fields
from clio_agent.gact.events import Event
from clio_agent.gact.parts import Part
from clio_agent.gact.session_store import _replace_session_messages
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.agent_tasks import AgentTask
    from clio_agent.gact.types import Message

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


# --------------------------------------------------------------------------- #
# D1: lazy per-session reconcile sweep for HISTORICAL stale parts
# --------------------------------------------------------------------------- #
#
# The terminal-transition writeback above (``reconcile_stored_handoff_part``,
# f7066068) closes every NEW started->terminal edge going forward, but a session
# whose child completed BEFORE that fix landed already has its part frozen
# "running" on disk -- no live fold will ever revisit it (the parent may never
# get another turn). Live evidence: sess_539d24da07bf, all 6 spawned children
# completed, three ``expert_handoff`` parts still read ``delegate.started`` /
# "running" on every ``GET /messages``.
#
# SEAM CHOICE: lazy, per-session, on first (re)load -- NOT a boot-time sweep over
# every stored session. ``ResidentLedgerSet`` (#889) deliberately replaced GACT's
# old eager "parse every messages/*.json before the port binds" boot path with a
# bounded LRU that materializes a session's ledger only when something actually
# reads it; that was the single biggest resident-memory win in the codebase. A
# boot-time reconcile sweep would have to either duplicate that full body walk
# (regressing #889) or re-derive it from the boot session INDEX alone, which
# carries no part data to inspect. Hooking ``ResidentLedgerSet``'s existing
# cache-miss seam instead costs nothing extra: the session's body is ALREADY
# being paged in for a real reader, so piggy-backing one more pass over the
# rows it just parsed is free, and this is the ONLY session shape a user could
# still observe "running" on (anything never read again is never rendered
# either).


_HANDOFF_RECONCILE_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "stale": {
        "category": "handoff_reconcile",
        "policy": "reconcile_on_session_load",
        "description": (
            "A stored expert_handoff part was still delegate.started ('running') "
            "for a task the agent-task registry already reports terminal -- the "
            "child completed BEFORE the terminal-transition writeback "
            "(task_fold.finish_agent_task_transition -> "
            "reconcile_stored_handoff_part, f7066068) ever ran for it, so it was "
            "never closed live. Reconciled the first time this session's ledger "
            "is (re)loaded into the resident set, through the SAME "
            "reconcile_stored_handoff_part path the live writeback uses."
        ),
    },
}


def handoff_reconciled_stale_payload(
    *, session_id: str, handle_id: str, task_status: str
) -> dict[str, Any]:
    """Build the typed ``handoff.reconciled.stale`` provenance payload.

    Mirrors :func:`clio_agent.gact.resident_ledgers.resident_ledger_reason_payload`:
    a closed reason catalog supplies the static category/description, folded with
    the dynamic per-repair identity, so a repair is queryable after the fact --
    never a silent read-time rewrite.

    Args:
        session_id: The parent session whose stored part was reconciled.
        handle_id: The reconciled ``expert_handoff`` part's ``handle_id``.
        task_status: The terminal :class:`~clio_agent.gact.agent_tasks.AgentTask`
            status that justified the reconcile (``completed``/``failed``/``cancelled``).

    Returns:
        A self-describing payload: ``event``, ``reason``, the dynamic identity
        fields, plus the reason catalog's static ``category``/``policy``/
        ``description``.
    """

    definition = _HANDOFF_RECONCILE_REASON_DEFINITIONS["stale"]
    return {
        "event": "handoff.reconciled.stale",
        "reason": "stale",
        "session_id": session_id,
        "handle_id": handle_id,
        "task_status": task_status,
        **dict(definition),
    }


def _record_handoff_reconciliation(app: "FastAPI", payload: dict[str, Any]) -> None:
    """Record one reconciliation to the trace plane + a bounded per-app audit ring.

    Mirrors :func:`clio_agent.gact.resident_ledgers._record_resident_audit`'s
    two-sink shape: ``trace.event`` for the log-plane, plus a small bounded deque
    on ``app.state`` so the repair is queryable via the process, not just grep-able
    in logs (no silent fallback).
    """

    trace.event(
        "HANDOFF-RECONCILE",
        "handoff.reconciled.stale session=%s handle=%s task_status=%s",
        payload.get("session_id", ""),
        payload.get("handle_id", ""),
        payload.get("task_status", ""),
    )
    ring = getattr(app.state, "handoff_reconciliations", None)
    if ring is None:
        ring = deque(maxlen=256)
        app.state.handoff_reconciliations = ring
    ring.append(payload)


def sweep_stale_handoff_parts(app: "FastAPI", session_id: str, messages: list["Message"]) -> int:
    """Reconcile HISTORICAL stale ``delegate.started`` parts on one session's ledger.

    Wired as :class:`~clio_agent.gact.resident_ledgers.ResidentLedgerSet`'s
    ``on_rehydrate`` hook, so this runs exactly once per cache-miss materialization
    -- a session's first load since boot, or since its resident copy was last
    evicted. See the module-level seam-choice note above for why lazy-per-session
    (not a boot-time full sweep) is the right call here.

    Cheap for the overwhelming common case: a single pass over the just-
    materialized rows collects candidate ``handle_id``s BEFORE touching the
    agent-task registry at all (the "skip sessions with no running-stage parts
    -- check before loading task records" requirement) -- only a session that
    actually carries a ``delegate.started`` expert_handoff part pays for a
    registry lookup.

    Idempotent: the actual repair is delegated to
    :func:`reconcile_stored_handoff_part` -- the SAME function the live
    terminal-transition writeback calls -- whose own idempotency (a part whose
    ``stage`` is no longer ``"delegate.started"`` is a no-op) means re-running
    this sweep on an already-reconciled session (by a prior sweep, or a live
    fold that ran since) never double-writes the part or double-publishes
    ``message.part.updated``. A successful repair additionally records ONE typed
    ``handoff.reconciled.stale`` provenance row
    (:func:`handoff_reconciled_stale_payload`) -- distinct from, and in addition
    to, the live path's own SSE publish -- so a lazy sweep-time repair is
    explicit and queryable, never a silent read-time rewrite.

    Args:
        app: The GACT app (agent-task registry + message store on ``app.state``).
        session_id: The session whose ledger was just materialized.
        messages: The freshly materialized ledger (used only for the cheap
            candidate scan -- the actual reconcile re-reads through
            ``app.state.messages``, so it always mutates the resident copy of
            record rather than this possibly-stale local snapshot).

    Returns:
        The number of parts reconciled (0 for the common no-op case).

    Never raises: called synchronously from a resident-ledger cache miss on the
    ``GET /messages`` read path (and every other reader), so a failure here must
    degrade to a typed, logged no-op rather than break the read -- the same
    discipline :func:`reconcile_stored_handoff_part` documents for itself.
    """

    try:
        return _sweep_stale_handoff_parts(app, session_id, messages)
    except Exception as exc:  # noqa: BLE001 - a read-path hook must never crash the read
        logger.warning(
            "stale expert_handoff sweep failed reason=sweep_error session=%s err=%r",
            session_id,
            exc,
        )
        return 0


def _sweep_stale_handoff_parts(
    app: "FastAPI", session_id: str, messages: list["Message"]
) -> int:
    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return 0

    stale_handle_ids = {
        str(part.handle_id or "")
        for message in messages
        for part in message.parts
        if part.type == "expert_handoff" and part.stage == "delegate.started" and part.handle_id
    }
    if not stale_handle_ids:
        return 0

    reconciled = 0
    for handle_id in stale_handle_ids:
        task = registry.get(handle_id)
        if task is None or not task.is_terminal:
            continue
        if reconcile_stored_handoff_part(app, task):
            reconciled += 1
            _record_handoff_reconciliation(
                app,
                handoff_reconciled_stale_payload(
                    session_id=session_id, handle_id=handle_id, task_status=task.status
                ),
            )
    return reconciled
