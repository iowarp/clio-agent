"""Turn start, off the loop (#1334): everything between "the turn task started" and
"forward" that blocks on the ARC store or runs operator hooks.

Formerly the inline prologue of ``turn.py::_run_turn_in_background``, which ran ON the
server loop: the user message's transcript persist (deferred here from the accept path),
the ``turn.started`` semantic event, context enrichment (a memory search is a BM25 store
RPC), the ``memory.search.completed`` event, and the ``UserPromptSubmit`` hooks (operator
subprocesses). Each store write is ~90 ms of ``threading.wait`` on the loop; the live
#1334 gate measured ~1.7 s per turn start. The orchestrator now awaits
:func:`prepare_turn_off_loop` through ``turn_forward._run_turn_setup_off_loop`` (turn
executor, contextvars copied), the same rail the forward and finalize use.

Order is preserved exactly: the transcript job first (so the user message's atoms
precede everything the turn appends), then the event, enrichment, the frame, the search
event, the hooks. The ``run_user_prompt_submit`` outcome is returned; anything but
``"proceed"`` ends the turn in the orchestrator, as before. An attached-context failure
is carried on ``state.context_file_error`` and raised at the commit-to-run seam.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from clio_agent.gact.enrichment import (
    _context_file_turn_provenance,
    _record_context_frame,
    enrich_turn_context,
    inject_pending_agent_task_notifications,
)
from clio_agent.gact.events import _publish_transcript_event
from clio_agent.gact.off_loop import schedule_off_loop
from clio_agent.gact.part_atom_minter import open_turn_minter
from clio_agent.gact.plan_mode import inject_plan_mode_reminder
from clio_agent.gact.replanning import inject_replan_suggestion
from clio_agent.gact.runtime import bringup_timing
from clio_agent.gact.runtime.globals import (
    _ContextFileAccessError,
    _emit_semantic_event,
    _session_agent_id,
)
from clio_agent.gact.session_store import _compile_session_conversation_history
from clio_agent.gact.todos import inject_todo_recitation
from clio_agent.gact.turn_state import DeferredTranscriptJob
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState

logger = logging.getLogger(__name__)

#: Typed reason for a deferred user-message persist the turn never got to run.
DEFERRED_JOB_ORPHANED = "deferred_user_message_persist_orphaned"


def spawn_user_turn(
    app: Any,
    session_id: str,
    *,
    turn_id: str,
    transcript_job: Optional[Callable[[], None]],
    run_turn: Callable[..., Any],
    args: tuple[Any, ...],
) -> Any:
    """Spawn the turn task through the ``TurnRunner``, guarding its deferred persist.

    The task's done-callback flushes a :class:`DeferredTranscriptJob` the turn never
    claimed (it never reached its prologue), off the loop and with a typed audit. Ordering
    is safe by construction: an unclaimed job means the prologue never ran, so the turn
    appended no atoms of its own for this message to land behind.
    """

    holder = DeferredTranscriptJob(transcript_job)
    task = app.state.turn_runner.spawn(
        run_turn(*args, transcript_job=holder), sid=session_id, turn_id=turn_id
    )

    def _flush_orphaned_job(_task: Any) -> None:
        leftover = holder.take()
        if leftover is None:
            return  # the prologue claimed and ran it, as it does on every normal turn
        stream_audit(
            "transcript.deferred_job_orphaned",
            session_id=session_id,
            turn_id=turn_id,
            reason=DEFERRED_JOB_ORPHANED,
        )
        logger.warning(
            "turn %s never reached its prologue; flushing the user message's deferred "
            "transcript persist off the loop (%s)",
            turn_id,
            DEFERRED_JOB_ORPHANED,
        )
        schedule_off_loop(leftover, label=f"transcript.orphaned_user_message:{turn_id}")

    add_done_callback = getattr(task, "add_done_callback", None)
    if add_done_callback is None:
        # The runner handed back no task handle (a test double that discards the
        # coroutine): there is no turn to run the prologue, so flush now. ``take()``
        # makes a double persist impossible even if a turn does materialise.
        _flush_orphaned_job(task)
    else:
        add_done_callback(_flush_orphaned_job)
    return task


def prepare_turn_off_loop(state: "TurnState", *, update_retry_attempt: Callable[..., Any]) -> str:
    """Run the turn prologue on the executor. Returns the ``UserPromptSubmit`` outcome.

    Raises whatever the deferred transcript job raises (``TranscriptIngestError``: the
    user message could not be persisted — the orchestrator settles an error turn, never
    a half-committed transcript).
    """

    # #1334 / #1337: the session's minter takes every later deferred persist (a steer,
    # an a2ui part); the user message's own persist runs first, inline, must-succeed.
    open_turn_minter(state.app, state.sid, state.turn_id)
    job = state.transcript_job.take() if state.transcript_job is not None else None
    if job is not None:
        job()

    _emit_semantic_event(
        state.app,
        state.sid,
        "turn.started",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="running",
        summary="User turn accepted and CLIO runtime started.",
        actor={"role": "user"},
        subject={"message_id": state.user_msg.id},
        payload={"text": state.user_text, "retry_attempt_id": state.retry_attempt_id},
    )
    _publish_transcript_event(
        state.bus,
        state.sid,
        "turn.started",
        {
            "turn_id": state.turn_id,
            "agent_id": state.turn_agent_id or _session_agent_id(state.sess) or "main",
        },
    )
    bringup_timing.timer_for_session(state.app, state.sid).end_phase("turn.accept_gap")

    # iowarp/clio-agent#5: prepend attached context files to the user's text so the
    # agent's forward() sees them as primed input (plain concat, expert-agnostic).
    state.context_file_error = None
    state.context_file_provenance = _context_file_turn_provenance(
        state.app, state.sid, status="prepared"
    )
    state.memory_search_metadata = {}
    try:
        # #1215 S5: enrich_turn_context times BOTH mechanisms below as ONE
        # "enrichment" bring-up phase (owner module gact/enrichment.py).
        state.enriched_text, state.memory_search_metadata = enrich_turn_context(
            state.app, state.sid, state.user_text, state.user_msg
        )
        # #948 S6 [1]/[4]: surface prior-turn background task results (observe-later).
        # STAGE the ids only; consumption + terminal emission defer to the commit-to-
        # run seam, so a turn aborted after enrichment leaves them pending.
        state.enriched_text, state.pending_notification_task_ids = (
            inject_pending_agent_task_notifications(state.app, state.sid, state.enriched_text)
        )
        # P1.2 #1064: surface plan mode to the model each turn (survives compaction; no-op otherwise).
        state.enriched_text = inject_plan_mode_reminder(
            state.app, state.sid, state.sess, state.enriched_text
        )
        state.enriched_text = inject_todo_recitation(
            state.app, state.sid, state.sess, state.enriched_text
        )
        # P1.6d #1068: surface a pending stall-triggered replanning suggestion once (no-op otherwise).
        state.enriched_text = inject_replan_suggestion(
            state.app, state.sid, state.sess, state.enriched_text
        )
        # Carry prior turns so a follow-up ("now plot it") reuses resolved state (no-op turn 1).
        state.enriched_text = _compile_session_conversation_history(
            state.app, state.sid, state.enriched_text
        )
    except _ContextFileAccessError as exc:
        state.enriched_text = state.user_text
        state.context_file_error = exc.error_info
        state.context_file_provenance = _context_file_turn_provenance(
            state.app, state.sid, status="error"
        )
    state.context_frame = _record_context_frame(
        state.app,
        state.sid,
        state.sess,
        state.user_msg,
        user_text=state.user_text,
        enriched_text=state.enriched_text,
        context_error=state.context_file_error,
    )
    if state.memory_search_metadata:
        _emit_semantic_event(
            state.app,
            state.sid,
            "memory.search.completed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            summary="Requested memory search was injected into turn context.",
            actor={"role": "runtime", "component": "memory"},
            subject={"message_id": state.user_msg.id},
            payload=state.memory_search_metadata,
        )
    # P2.2 #1070 / P2.6 #1074: UserPromptSubmit hooks (the ported ``pre_message``
    # consumer). A deny VETOES the turn (session -> error); a ``defer`` SUSPENDS it for
    # out-of-band approval (waiting_user, resume as a new turn). The whole finalize-
    # boundary protocol lives in the hooks owner module (no-accretion) — this is only
    # the call site: any non-"proceed" outcome ends the turn in the orchestrator.
    if state.context_file_error is not None:
        return "proceed"
    from clio_agent.gact.hooks.user_prompt import run_user_prompt_submit  # noqa: PLC0415

    return run_user_prompt_submit(state, update_retry_attempt=update_retry_attempt)


__all__ = ["DEFERRED_JOB_ORPHANED", "prepare_turn_off_loop", "spawn_user_turn"]
