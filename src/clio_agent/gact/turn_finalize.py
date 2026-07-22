"""Finalize seam for the GACT turn engine (#767 Phase B).

Slice 6 of the ``turn.py`` decomposition — the last and most entangled. Three
free functions taking :class:`~clio_agent.gact.turn_state.TurnState` first (the
gact seam convention) move here:

* :func:`maybe_pause_for_user` — the ask-user early-return path. When the
  prediction carries an ``ask_user`` action and the turn has no error, it mints
  the :class:`~clio_agent.gact.types.UserQuestion`, flips the session to
  ``waiting_user``, settles the ledger, and returns ``True`` so the orchestrator
  returns before the finalize region. Otherwise it returns ``False`` and the
  turn proceeds.
* :func:`finalize_turn` — everything after the forward except-chain: answer
  grounding, assistant-part assembly (route banner, wrap-up thinking, canonical
  answer channel, file diffs), diff indexing, nanoagent spawn, the terminal
  publishes, persistence, retry bookkeeping, and the post_message hook. The
  orchestrator runs THIS under the #756 ``try/except finalize_exc`` envelope; a
  crash here is settled by :func:`settle_failed_finalize`.
* :func:`settle_failed_finalize` — the #756 error envelope for a finalize-region
  crash (relocated verbatim from ``turn.py``): structured log, ``turn.failed``
  semantic event, ``message.completed`` with ``stop_reason=error``, a persisted
  assistant error message, ledger freeze/retire, and a terminal
  ``session.status_changed``. Nothing degrades silently.

Behavior is byte-for-byte identical to the former in-``turn.py`` body. The two
small per-turn closures the orchestrator still owns —
``_drain_observed_tool_calls`` and ``_update_retry_attempt`` (both read by the
body AND by finalize) — are threaded in by reference, exactly as
``settle_failed_finalize`` already took ``update_retry_attempt`` (the #714 seam
pattern).

The #714 danger set (``_append_session_message`` /
``_enrich_cancellation_error_info``, retargeted by ~83 ``app._X`` test
monkeypatches) is resolved through ``app`` via a *function-local* import at each
call site so those monkeypatches keep intercepting with zero test edits.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.resolution import (
    _runtime_active_agent_blueprint_id,
)
from clio_agent.gact.artifacts.minting import clear_turn_artifacts
from clio_agent.gact.artifacts.cas_gc import finalize_cas_budget_check
from clio_agent.gact.artifacts.wire import append_turn_resource_links, proposed_diff_payload
from clio_agent.gact.delegation import (
    _produced_turn_workflow_state,
    _workflow_state_from_handoff_rows,
)
from clio_agent.gact.enrichment import _finalize_context_frame
from clio_agent.gact.events import Event, EventBus, _publish_transcript_event
from clio_agent.gact.evidence import (
    _ground_fabricated_local_artifact_paths,
    _tool_result_preview,
)
from clio_agent.gact.messaging import (
    _ask_user_options_from_action,
    _coerce_ask_user_action,
)
from clio_agent.gact.runtime.globals import (
    _emit_semantic_event,
    _iso_from_epoch,
    _new_message_id,
    _new_part_id,
    _new_question_id,
    _session_agent_id,
)
from clio_agent.gact.runtime.retention import enforce_list_bound
from clio_agent.gact.streaming import (
    _pop_stream_fallback,
    _stream_fallback_payload,
)
from clio_agent.gact.tool_observer import (
    _sanitize_handoff_tool_metadata,
    _sanitize_tools_called_metadata,
)
from clio_agent.gact.transcript_projection import final_message_embed
from clio_agent.gact.turn_stream import assemble_stream_metadata, settle_turn_transcript
from clio_agent.gact.types import (
    ErrorInfo,
    Message,
    Part,
    Tokens,
    UserQuestion,
)
from clio_agent.gact.usage import capture_reasoning_log
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from clio_agent.gact.turn_state import TurnState

logger = logging.getLogger(__name__)


def maybe_pause_for_user(
    state: "TurnState",
    pred: Any,
    *,
    update_retry_attempt: "Callable[..., None]",
) -> bool:
    """Pause the turn for user input when the prediction requested it (#767 Phase B).

    When the prediction carries an ``ask_user`` action and the turn has not
    already errored, this mints the :class:`~clio_agent.gact.types.UserQuestion`,
    flips the session to ``waiting_user``, finalizes the context frame, settles
    the transcript ledger, and returns ``True`` — the orchestrator then returns
    before the finalize region (the in-flight assistant identity/parts stay in
    the legacy dicts so the resume turn re-adopts them). Returns ``False`` when
    there is nothing to pause for and the turn should proceed to finalize.

    ``update_retry_attempt`` is the orchestrator's per-turn closure, threaded in
    by reference (it is also read by the linear body + the finalize region).
    """

    ask_user_action = _coerce_ask_user_action(pred)
    if state.error_info is not None or not ask_user_action:
        return False
    now_iso = datetime.now(timezone.utc).isoformat()
    options = _ask_user_options_from_action(ask_user_action)
    kind_raw = str(ask_user_action.get("kind") or "").strip()
    kind = kind_raw if kind_raw in {"freeform", "choice", "confirmation"} else ""
    if not kind:
        kind = "choice" if options and not ask_user_action.get("allow_freeform") else "freeform"
    question = UserQuestion(
        id=_new_question_id(),
        session_id=state.sid,
        prompt=str(ask_user_action["question"]),
        status="pending",
        kind=kind,  # type: ignore[arg-type]
        options=options,
        created_at=now_iso,
        updated_at=now_iso,
        source="orchestrator_action",
        turn_id=state.user_msg.id,
        attempt_id=state.retry_attempt_id,
        metadata={
            **dict(ask_user_action.get("metadata") or {}),
            "reason": ask_user_action.get("reason", ""),
            "caller": ask_user_action.get("caller", {}),
            "resume_on_answer": True,
            "source_user_message_id": state.user_msg.id,
            "source_user_text": state.user_text,
            "selected_agent": state.selected_agent,
            "route_source": state.route_source,
            "route_reason": state.route_reason,
        },
    )
    state.app.state.user_questions[question.id] = question
    _emit_semantic_event(
        state.app,
        state.sid,
        "user_question.created",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="waiting_user",
        summary="Agent requested user input before continuing.",
        actor={"agent_id": state.selected_agent or state.invocation_agent_id},
        subject={"question_id": question.id},
        payload=question.model_dump(exclude_none=True),
    )
    updated = state.app.state.sessions.update(
        state.sid,
        status="waiting_user",
        message_count=len(state.app.state.messages.get(state.sid, [])),
        metadata_patch={"pending_user_question_id": question.id},
    )
    _finalize_context_frame(
        state.app,
        state.sid,
        state.context_frame["id"],
        "",
        "completed",
        error_info=None,
    )
    state.bus.publish(
        Event(
            type="user_question.created",
            session_id=state.sid,
            payload=question.model_dump(exclude_none=True),
        )
    )
    state.bus.publish(
        Event(
            type="session.status_changed",
            session_id=state.sid,
            payload={
                "session_id": state.sid,
                "status": "waiting_user",
                "prev_status": "running",
                "updated_at": updated.updated_at if updated is not None else "",
                "pending_user_question_id": question.id,
            },
        )
    )
    if state.retry_attempt_id:
        update_retry_attempt(
            "completed",
            metadata_patch={
                "ask_user_question_id": question.id,
                "stop_reason": "waiting_user",
            },
        )
    # #767 PR2: the ask_user pause exits the turn before the finalize
    # region — settle the ledger so it can never poison a later turn.
    # The in-flight assistant identity/parts stay in the legacy dicts
    # (deliberately NOT popped here, exactly as before) and the resume
    # turn re-adopts them via _open_turn_transcript's carried-state
    # adoption, so the resumed turn continues the same assistant
    # message without a second message.created.
    settle_turn_transcript(state)
    return True


def finalize_turn(
    state: "TurnState",
    pred: Any,
    *,
    drain_observed_tool_calls: "Callable[[list[dict[str, Any]]], list[dict[str, Any]]]",
    update_retry_attempt: "Callable[..., None]",
) -> None:
    """Assemble, publish, and persist the turn's assistant message (#767 Phase B).

    Everything after the forward except-chain: answer grounding, assistant-part
    assembly (route banner, wrap-up thinking, the canonical answer channel, file
    diffs), diff indexing, nanoagent spawn, the terminal semantic + bus
    publishes, persistence + settle, retry bookkeeping, and the post_message
    hook. Byte-for-byte the former in-``turn.py`` finalize body.

    The orchestrator runs this UNDER the #756 ``try/except finalize_exc``
    envelope; an exception escaping here is settled by
    :func:`settle_failed_finalize`. ``drain_observed_tool_calls`` and
    ``update_retry_attempt`` are the orchestrator's per-turn closures, threaded
    in by reference (both are also read by the linear body).
    """

    # #714 danger set: bind through app at call time so test monkeypatches of
    # clio_agent.gact.app._enrich_cancellation_error_info /
    # _append_session_message keep intercepting with zero test edits.
    from clio_agent.gact.app import (  # noqa: PLC0415
        _append_session_message,
        _enrich_cancellation_error_info,
    )

    # Final user-facing text only: correct any fabricated local artifact path the
    # answer presents as produced — whether the synthesizing expert composed a
    # plausible-but-wrong filename or the delegation-fallback text carried a
    # model-requested ``output_path`` that the tool never wrote — by grounding it
    # against the run's verified on-disk artifacts in the merged typed
    # workflow_state. Generic (pack schema + filesystem only), applied once on the
    # assembled answer, never on intermediate child rows.
    if state.answer_text and state.expert_handoffs:
        state.answer_text = _ground_fabricated_local_artifact_paths(
            state.answer_text,
            _workflow_state_from_handoff_rows(state.expert_handoffs, schema=state.workflow_schema),
            schema=state.workflow_schema,
        )

    # Build assistant parts — routing_decision (v0.2) first when we
    # got a selected_agent, then optional thinking trace, then the
    # text answer, then any file_diffs.
    if (
        state.error_info is None
        and not state.answer_text
        and not state.thinking_text
        and not state.proposed_diffs
        and not state.nanoagents
    ):
        state.error_info = ErrorInfo(
            error="empty_response",
            message="Agent completed without user-visible output.",
            details={
                "session_id": state.sid,
                "routing_mode": getattr(state.sess, "routing_mode", "auto"),
                "selected_agent": state.selected_agent,
            },
            recoverable=True,
        )

    # ---- #767 PR3: finalize is a READER of the TurnTranscript ledger. ----
    # Live parts already streamed at the moment they happened; finalize only
    # appends ITS OWN parts (route banner, wrap-up thinking, the canonical
    # answer channel, file diffs) through the same producer API and then
    # persists the ledger verbatim. No live-parts scans, no rebuild-from-rows,
    # no text swap, no dedup, no re-publish.
    #
    # Capture stream provenance BEFORE any finalize-time append: an atomic
    # append is the runtime boundary and clears ``current_stream_part_id``
    # (the legacy closure var was only reset by mid-turn boundaries).
    current_stream_part_id = state.transcript.current_stream_part_id
    live_assistant_parts = state.transcript.snapshot()
    has_live_parts = bool(live_assistant_parts or current_stream_part_id)
    live_tool_calls = {
        p.call_id: p for p in live_assistant_parts if p.type == "tool_call" and p.call_id
    }
    for part in live_assistant_parts:
        if part.type != "tool_result" or not part.call_id:
            continue
        call_part = live_tool_calls.get(part.call_id)
        if call_part is None:
            continue
        for row in state.tools_called:
            if str(row.get("name") or "") != call_part.tool_name:
                continue
            if row.get("args") != call_part.input:
                continue
            if "result" not in row:
                continue
            part.content = [
                Part(
                    id=f"{part.id}_final_text",
                    type="text",
                    text=_tool_result_preview(row.get("result")),
                )
            ]
            break

    # The expert that produced this turn's thinking/answer/diff parts: the routed
    # expert when one was selected, else the active orchestrator.
    responder_agent_id = state.selected_agent or state.invocation_agent_id or "main"
    # Take the canonical-answer channel FIRST: its exactly-once identity seeds
    # from the pre-append ledger (the still-open streamed answer part included).
    # It covers the responder PLUS the stream tap's attribution fallback label
    # (``emit_chunk``'s chat-path default) — the same top-level LM call's
    # answer can stream under either; a delegated child's channel is NOT
    # covered (its deliverable settled at its LM-call site and must never
    # suppress the responder's distinct final answer).
    answer_channel = state.transcript.turn_answer_stream(
        responder_agent_id,
        state.active_agent_id or state.invocation_agent_id or "main",
    )
    # Mechanism 1 replaced: the once-key IS the identity — the same
    # ``route:{agent}`` key the live tool observer uses, so the banner lands
    # exactly once whether it streamed live or lands here.
    if state.selected_agent:
        state.transcript.append_part_once(
            f"route:{state.selected_agent}",
            Part(
                id=_new_part_id(),
                type="routing_decision",
                # The decision is MADE by the orchestrator; ``selected_agent`` is the
                # CHOSEN expert.
                agent_id=state.invocation_agent_id or "main",
                metadata={
                    k: v
                    for k, v in {
                        "route_source": state.route_source,
                        "route_reason": state.route_reason,
                    }.items()
                    if v
                },
                selected_agent=state.selected_agent,
                rationale=state.rationale,
                confidence=0.0,
                heuristic=False,
                execution_path=state.execution_path,
            ),
            stream_source="batch",
        )
    # Mechanism 2 replaced: there is no finalize rebuild-from-rows — delegation
    # appended its expert_handoff parts once, at emit time; the
    # ``expert_handoffs`` rows stay message METADATA only (design §4 row 6).
    #
    # iowarp/clio-agent#17: surface DSPy reasoning as a thinking Part so the TUI
    # can collapse + render it. Gated by op identity (has_closed_text replaces
    # the suppressed_thinking_part substring matching): when the responder's
    # contract reasoning already streamed live as a text part this turn, the
    # wrap-up copy is that same channel and must not land twice.
    #
    # Close the still-open streamed part FIRST: ``has_closed_text`` reads
    # closed state only, and on the chat path (no selected_agent -> the
    # routing-banner append above never ran to close it) a turn that
    # streamed ``reasoning`` and returned a batch-only answer still holds
    # that reasoning part OPEN here — the gate saw "nothing landed" and
    # appended a verbatim batch ``thinking`` twin (the #732 duplicate
    # class). On routed turns the banner's ``append_part`` already closed
    # it, so this is a no-op there. An explicit close deliberately does
    # NOT reset ``current_stream_part_id`` (captured above), so the
    # live-vs-batch stream provenance below is unchanged; the canonical
    # answer channel was taken above, while its part could still be open.
    state.transcript.close_open_text()
    if state.thinking_text and not state.transcript.has_closed_text(
        responder_agent_id, "reasoning"
    ):
        state.transcript.append_part(
            Part(
                id=_new_part_id(),
                type="thinking",
                agent_id=responder_agent_id,
                text=state.thinking_text,
            ),
            stream_source="batch",
        )
    # Mechanisms 4+5 replaced: the canonical turn answer settles its exactly-once
    # channel — when an ``answer``-field part already landed this turn (streamed
    # live and closed with its own cleaned buffer, or a terminal expert's batch
    # burst), the fallback is audited + ignored BY OP IDENTITY; otherwise ONE
    # batch added+completed burst lands now. Never both; never a text swap
    # (the streamed part's close already carried the cleaned buffer as
    # final_text — there is nothing to swap). The turn responder is a react main
    # whose ``answer`` IS the user deliverable, so its batch fallback always lands.
    stream_fallback = _pop_stream_fallback(state.app, state.sid)
    batch_turn_text = current_stream_part_id is None
    if (
        batch_turn_text
        and (bool(state.answer_text) or state.error_info is not None)
        and not stream_fallback
    ):
        stream_fallback = _stream_fallback_payload("sync_execution_path")
    answer_channel.finish(
        fallback_text=str(state.answer_text or ""),
        fallback_metadata=(
            {"stream_fallback": stream_fallback} if stream_fallback and batch_turn_text else {}
        ),
    )
    for row in state.proposed_diffs:
        if isinstance(row, dict):
            getf = row.get
        else:

            def getf(k, default=None, _r=row):
                return getattr(_r, k, default)

        path = getf("path", "") or ""
        udiff = getf("unified_diff", "") or ""
        new_content = getf("new_content", "") or ""
        edit_mode = getf("edit_mode", "") or ""
        lines_added = int(getf("lines_added", 0) or 0)
        lines_removed = int(getf("lines_removed", 0) or 0)
        if not path:
            continue
        # In "whole" mode the unified_diff may be empty by design;
        # the new_content carries the full replacement. Accept either
        # so the Part lands instead of being dropped.
        if not udiff and not new_content:
            continue
        diff_part = Part(
            id=_new_part_id(),
            type="file_diff",
            agent_id=responder_agent_id,
            path=path,
            unified_diff=udiff,
            new_content=new_content,
            status="pending",
            edit_mode=edit_mode,
            lines_added=lines_added,
            lines_removed=lines_removed,
        )
        state.transcript.append_part(diff_part, stream_source="batch")
        _emit_semantic_event(
            state.app,
            state.sid,
            "artifact.proposed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            summary=f"Agent proposed a file diff for {path}.",
            actor={"agent_id": state.selected_agent or state.invocation_agent_id},
            subject={"path": path, "part_id": diff_part.id, "artifact_type": "file_diff"},
            payload=proposed_diff_payload(
                path, udiff, new_content, edit_mode, lines_added, lines_removed
            ),
        )

    # #968 item 2: give every artifact GENERATED this turn outbound wire identity —
    # one ``resource_link`` part per new version (owner decision #966.9), owned by
    # the artifacts package so finalize stays a one-line caller.
    append_turn_resource_links(
        state.app, state.sid, state.turn_id, state.transcript, agent_id=responder_agent_id
    )

    # #972: cheap post-turn CAS budget check (owner-module guarded one-liner) — a
    # running total vs the budget; the reachability eviction scan runs ONLY on a breach.
    finalize_cas_budget_check(state.app, state.sess, state.sid)

    state.error_info = _enrich_cancellation_error_info(state.app, state.sid, state.error_info)
    state.cancelled_turn = state.error_info is not None and state.error_info.error == "cancelled"
    if state.cancelled_turn:
        state.app.state.cancel_flags.discard(state.sid)
        ledger = getattr(state.app.state, "tool_call_ledger", None)
        if ledger is not None:
            ledger.pop(state.sid, None)

    state.assistant_metadata = {}
    if state.turn_agent_id:
        state.assistant_metadata["agent_override"] = {
            "requested_agent_id": state.turn_agent_id,
            "session_agent_id": _session_agent_id(state.sess),
            "effective_agent_id": state.selected_agent or state.turn_agent_id,
            "scope": "turn",
        }
    # Stamp stream provenance (verbatim behaviour): mark the turn's text live vs
    # batch and, on a batch answer, record the delivery-path stream_fallback payload.
    assemble_stream_metadata(
        state,
        stream_fallback=stream_fallback,
        current_stream_part_id=current_stream_part_id,
        has_live_parts=has_live_parts,
    )
    # A live observer completion can arrive after the immediate post-forward drain
    # but before the assistant message is persisted. Reconcile once more at the
    # final metadata boundary so reloads retain the same tool facts as the live bus.
    if not state.cancelled_turn:
        state.tools_called = drain_observed_tool_calls(state.tools_called)
    state.tools_called = _sanitize_tools_called_metadata(state.tools_called)
    if state.tools_called:
        state.assistant_metadata["tools_called"] = state.tools_called
    if state.expert_handoffs:
        state.expert_handoffs = [
            _sanitize_handoff_tool_metadata(row) if isinstance(row, Mapping) else row
            for row in state.expert_handoffs
        ]
        state.assistant_metadata["expert_handoffs"] = state.expert_handoffs
    # #953: stamp the turn's produced typed workflow_state so a spawned child's
    # completion hook (turn_spawn._child_workflow_state, reading metadata["workflow_state"])
    # threads it back — root seam, all kinds (a chain_of_thought LEAF's field was dropped here).
    produced_wf = _produced_turn_workflow_state(
        state.pred, state.expert_handoffs, schema=state.workflow_schema
    )
    if produced_wf:
        state.assistant_metadata["workflow_state"] = produced_wf
    if state.context_file_provenance["files"]:
        state.assistant_metadata["context_files"] = state.context_file_provenance
    if state.memory_search_metadata:
        state.assistant_metadata["memory_search"] = state.memory_search_metadata
    if state.agent_runtime:
        state.assistant_metadata["agent_runtime"] = state.agent_runtime
    if state.prompt_resolution:
        state.assistant_metadata["prompt_resolution"] = state.prompt_resolution
    # #953 [5]: surface a declared variant's winner stamp (additive) on the metadata.
    if getattr(state.pred, "variant_selection", None):
        state.assistant_metadata["variant_selection"] = state.pred.variant_selection
    # Reasoning capture: persist per-call chain-of-thought onto the assistant
    # message metadata (owner: usage.capture_reasoning_log). Best-effort, gated
    # by CLIO_CAPTURE_REASONING; mutates state.assistant_metadata in place.
    capture_reasoning_log(state)
    # iowarp/clio-agent#6: the transcript is the sole minter of the assistant
    # message id — reuse it when a producer already minted it (stream tap /
    # tool observer / the finalize appends above); a turn with no parts at
    # all mints + publishes message.created here, exactly once.
    asst_id = state.transcript.ensure_message()
    # #767 PR3: persist the ledger VERBATIM. finalize() closes any still-open
    # streamed part (publishing its completed event with the cleaned buffer),
    # stamps the 1-based arrival-order ``sequence`` (#731: reload order IS
    # stream order, by construction), freezes the ledger against late
    # producers, and returns the parts. No text rewriting, no dedup, no
    # re-publish — live and reload are two projections of this one ledger.
    assistant_parts = state.transcript.finalize()
    assistant_msg = Message(
        id=asst_id,
        # Correlate the assistant reply to the user-turn that produced it (#711).
        turn_id=state.turn_id,
        session_id=state.sid,
        role="assistant",
        created_at=_iso_from_epoch(time.time()),
        updated_at=_iso_from_epoch(time.time()),
        parts=assistant_parts,
        tokens=Tokens(**state.turn_tokens),
        cost_usd=state.turn_cost,
        stop_reason="cancelled"
        if state.cancelled_turn
        else ("error" if state.error_info else "end_turn"),
        error_info=state.error_info,
        metadata=state.assistant_metadata,
    )
    _finalize_context_frame(
        state.app,
        state.sid,
        state.context_frame["id"],
        assistant_msg.id,
        "cancelled" if state.cancelled_turn else ("error" if state.error_info else "completed"),
        error_info=state.error_info,
    )

    # Index file_diff parts so /diffs/apply + /diffs/reject find them.
    bucket = state.app.state.pending_diffs.setdefault(state.sid, [])
    for p in assistant_parts:
        if p.type != "file_diff":
            continue
        write_content = (
            p.new_content if p.new_content or p.edit_mode in {"whole", "patch"} else None
        )
        bucket.append(
            {
                "path": p.path,
                "unified_diff": p.unified_diff,
                "new_content": write_content,
                "status": "pending",
                "part_id": p.id,
                "message_id": assistant_msg.id,
            }
        )
    enforce_list_bound(state.app, bucket, "pending_diffs", session_id=state.sid)

    # #767 PR3: finalize re-publishes NOTHING — every part's message.created /
    # part.added / part.delta / part.completed already went out at append
    # time, from the one producer API. Tool lifecycle events are only emitted
    # by the live observer at the execution boundary. Prediction.tools_called
    # remains summary metadata; do not reconstruct started/completed events
    # after the turn, because that makes post-hoc facts look like live tool
    # timing.
    completed_payload: dict[str, Any] = {
        "turn_id": state.turn_id,
        "message_id": assistant_msg.id,
        "stop_reason": "cancelled"
        if state.cancelled_turn
        else ("error" if state.error_info else "end_turn"),
        "tokens": dict(state.turn_tokens),
        "cost_usd": state.turn_cost,
    }
    if state.error_info is not None:
        completed_payload["error_info"] = state.error_info.model_dump(exclude_none=True)
    if state.assistant_metadata:
        completed_payload["metadata"] = state.assistant_metadata
    # #737 S5: the final_message byte-copy rides the DURABLE turn.completed only under
    # the LEGACY regime; the atoms regime derives it from the message_part atoms so the
    # byte-copy dies (embed -> {}). SSE strips it either way (SENSITIVE_KEYS).
    semantic_completed_payload = {
        **completed_payload,
        **final_message_embed(state.app, state.sid, assistant_msg),
    }
    _emit_semantic_event(
        state.app,
        state.sid,
        "turn.completed" if state.error_info is None else "turn.failed",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="completed" if state.error_info is None else "failed",
        summary=(
            "CLIO turn completed."
            if state.error_info is None
            else f"CLIO turn failed: {state.error_info.error}."
        ),
        actor={"agent_id": state.selected_agent or "orchestrator"},
        subject={"message_id": assistant_msg.id},
        payload=semantic_completed_payload,
    )
    _publish_transcript_event(
        state.bus,
        state.sid,
        "turn.completed",
        {"turn_id": state.turn_id},
    )
    state.bus.publish(
        Event(
            type="message.completed",
            session_id=state.sid,
            payload=completed_payload,
        )
    )

    # Persist + settle.
    final_status = (
        "cancelled" if state.cancelled_turn else ("error" if state.error_info else "idle")
    )
    retry_status = (
        "cancelled" if state.cancelled_turn else ("failed" if state.error_info else "completed")
    )
    _append_session_message(state.app, state.sid, assistant_msg)
    # #767 PR3: the ledger is already frozen by transcript.finalize(); settle
    # retires it from the registry so a late producer op is rejected +
    # audited, never absorbed silently.
    settle_turn_transcript(state)
    getattr(state.app.state, "live_assistant_message_ids", {}).pop(state.sid, None)
    getattr(state.app.state, "live_assistant_parts", {}).pop(state.sid, None)
    getattr(state.app.state, "live_assistant_part_keys", {}).pop(state.sid, None)
    update_retry_attempt(
        retry_status,
        metadata_patch={
            "executed_user_message_id": state.user_msg.id,
            "assistant_message_id": assistant_msg.id,
            "stop_reason": completed_payload["stop_reason"],
        },
    )
    state.app.state.sessions.update(
        state.sid,
        status=final_status,
        message_count=state.sess.message_count + 2,
        add_tokens_input=state.turn_tokens["input"],
        add_tokens_output=state.turn_tokens["output"],
        add_cost_usd=state.turn_cost,
    )
    cancellation_status: dict[str, Any] = {}
    if state.cancelled_turn and state.error_info is not None:
        cancellation_status = {
            "execution_cancellation": state.error_info.details.get("execution_cancellation"),
            "executor_work_may_continue": state.error_info.details.get(
                "executor_work_may_continue"
            ),
            "cancellation_attempt": state.error_info.details.get("cancellation_attempt", {}),
        }
    state.bus.publish(
        Event(
            type="session.status_changed",
            session_id=state.sid,
            payload={
                "session_id": state.sid,
                "status": final_status,
                "prev_status": "running",
                **cancellation_status,
            },
        )
    )
    # iowarp/clio-agent#20: post_message hook runs AFTER persistence
    # so user audit code sees the settled assistant + can ship to
    # external systems. Errors are swallowed (post_* contract).
    try:
        from clio_agent.runtime.hooks import fire as _fire_hook  # noqa: PLC0415

        _emit_semantic_event(
            state.app,
            state.sid,
            "hook.invocation.started",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="running",
            summary="post_message hook dispatch started.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={"assistant": assistant_msg.model_dump(exclude_none=True)},
        )
        _fire_hook(
            "post_message",
            state.sid,
            assistant_msg.model_dump(exclude_none=True),
            hook_scope={
                "session_id": state.sid,
                "workspace_id": getattr(state.sess, "workspace_id", ""),
                "blueprint_id": _runtime_active_agent_blueprint_id(state.app, state.sid),
            },
        )
        _emit_semantic_event(
            state.app,
            state.sid,
            "hook.invocation.completed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            summary="post_message hook dispatch completed.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={},
        )
    except Exception:  # noqa: BLE001
        _emit_semantic_event(
            state.app,
            state.sid,
            "hook.invocation.failed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="failed",
            summary="post_message hook dispatch failed and was swallowed by policy.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={},
        )
        pass
    if not (
        state.cancelled_turn
        and state.error_info is not None
        and state.error_info.details.get("execution_cancellation") == "best_effort"
    ):
        if state.app.state.cancel_events.get(state.sid) is state.turn_cancel_event:
            state.app.state.cancel_events.pop(state.sid, None)


def settle_failed_finalize(
    app: "FastAPI",
    sid: str,
    *,
    turn_id: str,
    trace_id: str,
    turn_tokens: Mapping[str, int],
    turn_cost: float,
    turn_cancel_event: threading.Event,
    update_retry_attempt: "Callable[..., None]",
    exc: BaseException,
) -> None:
    """#756: the turn's error envelope for a finalize-region crash.

    Everything after :func:`~clio_agent.gact.turn._run_turn_in_background`'s
    forward except-chain (answer grounding, part assembly, diff indexing,
    publishes, persistence) runs inside a fire-and-forget task. An exception
    escaping there used to die silently -- no ``message.completed``, no
    ``session.status_changed``, session wedged in ``running`` forever. This
    settles the turn instead: structured log, ``turn.failed`` semantic event,
    ``message.completed`` with ``stop_reason=error`` + ``error_info``, a
    persisted assistant error message (so the failure is visible in the reloaded
    transcript, not just live), and a terminal ``session.status_changed``.
    Nothing degrades silently: every best-effort step below logs its reason when
    it fails.
    """

    # #714 danger set: bind through app at call time so test monkeypatches of
    # clio_agent.gact.app._append_session_message (e.g. the live==reload
    # property fixture) keep intercepting assistant persistence.
    from clio_agent.gact.app import _append_session_message  # noqa: PLC0415

    logger.error(
        "turn finalize failed: reason=turn_finalize_error session=%s turn=%s error=%s",
        sid,
        turn_id,
        type(exc).__name__,
        exc_info=exc,
    )
    if trace.HF_ON:
        trace.hot("TURN-FINALIZE-FAIL", "%s %s: %s", sid, type(exc).__name__, exc)

    # #767 PR2: a failed finalize must still settle the ledger — freeze it
    # (late producer ops are rejected + audited) and retire it from the
    # registry so it can never poison the next turn. Runs unconditionally,
    # before the already-settled early return below.
    registry = getattr(app.state, "turn_transcripts", None)
    if registry is not None:
        transcript = registry.get(sid)
        if transcript is not None:
            transcript.abandon()
        registry.close(sid)

    # A crashed finalize never reaches the resource_link drain; clear the turn's
    # artifact buffer so a retry of the SAME turn cannot emit each part twice (#968
    # finding [7]). Unconditional, before the already-settled early return.
    clear_turn_artifacts(app, sid)

    sess = app.state.sessions.get(sid)
    if sess is not None and getattr(sess, "status", "") != "running":
        # Finalize already settled the turn (the exception escaped after the
        # terminal publishes); re-running the envelope would double-publish
        # completion. The failure stays visible via the log above.
        return

    error_info = ErrorInfo(
        error="finalize_error",
        message=f"turn finalize raised: {exc}",
        details={
            "reason": "turn_finalize_error",
            "session_id": sid,
            "turn_id": turn_id,
            "original_error": type(exc).__name__,
            "stage": "finalize",
        },
        recoverable=True,
    )
    now = time.time()
    assistant_msg = Message(
        id=_new_message_id("asst"),
        turn_id=turn_id,
        session_id=sid,
        role="assistant",
        created_at=_iso_from_epoch(now),
        updated_at=_iso_from_epoch(now),
        parts=[],
        tokens=Tokens(**dict(turn_tokens)),
        cost_usd=turn_cost,
        stop_reason="error",
        error_info=error_info,
    )
    completed_payload: dict[str, Any] = {
        "turn_id": turn_id,
        "message_id": assistant_msg.id,
        "stop_reason": "error",
        "tokens": dict(turn_tokens),
        "cost_usd": turn_cost,
        "error_info": error_info.model_dump(exclude_none=True),
    }
    bus: EventBus = app.state.bus
    try:
        _emit_semantic_event(
            app,
            sid,
            "turn.failed",
            turn_id=turn_id,
            trace_id=trace_id,
            status="failed",
            summary=f"CLIO turn failed: {error_info.error}.",
            actor={"agent_id": "orchestrator"},
            subject={"message_id": assistant_msg.id},
            payload={
                **completed_payload,
                **final_message_embed(app, sid, assistant_msg),
            },
        )
    except Exception:  # noqa: BLE001 - the bus publishes below must still go out
        logger.exception(
            "turn.failed semantic emit failed during finalize settle: session=%s turn=%s",
            sid,
            turn_id,
        )
    _publish_transcript_event(bus, sid, "turn.completed", {"turn_id": turn_id})
    bus.publish(
        Event(
            type="message.completed",
            session_id=sid,
            payload=completed_payload,
        )
    )
    try:
        _append_session_message(app, sid, assistant_msg)
    except Exception:  # noqa: BLE001 - persistence degraded; the status flip must still happen
        logger.exception(
            "assistant error-message persistence failed during finalize settle: session=%s turn=%s",
            sid,
            turn_id,
        )
    try:
        update_retry_attempt(
            "failed",
            metadata_patch={
                "assistant_message_id": assistant_msg.id,
                "stop_reason": "error",
            },
        )
    except Exception:  # noqa: BLE001 - retry bookkeeping degraded; keep settling
        logger.exception(
            "retry-attempt update failed during finalize settle: session=%s turn=%s",
            sid,
            turn_id,
        )
    getattr(app.state, "live_assistant_message_ids", {}).pop(sid, None)
    getattr(app.state, "live_assistant_parts", {}).pop(sid, None)
    getattr(app.state, "live_assistant_part_keys", {}).pop(sid, None)
    if sess is not None:
        app.state.sessions.update(
            sid,
            status="error",
            message_count=sess.message_count + 2,
        )
    bus.publish(
        Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": "error",
                "prev_status": "running",
            },
        )
    )
    if app.state.cancel_events.get(sid) is turn_cancel_event:
        app.state.cancel_events.pop(sid, None)
