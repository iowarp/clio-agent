"""Dynamic-agent delegation settle engine for the GACT turn engine (#767 Phase B).

Slice 4 of the ``turn.py`` decomposition: the synchronous delegation *settle
engine* that used to live inline in ``_run_turn_in_background`` as a stack of
mutually recursive closures moves here as free functions taking
:class:`~clio_agent.gact.turn_state.TurnState` first (the gact seam convention).

The engine is behavior-preserving. Three functions cooperate:

* :func:`run_dynamic_agent_sync` runs ONE dynamic-agent (blueprint/prompt/tool)
  forward in an executor, emits its per-expert semantic events, settles a
  terminal expert's exactly-once ``answer`` channel, and captures a terminal
  CoT/predict expert's reasoning + answer into its own ARC scope.
* :func:`execute_delegated_experts` walks the model-emitted delegation rows,
  resolves + guards each child (availability / parent match / cycle / depth),
  runs it via :func:`run_dynamic_agent_sync`, and recurses into the child's own
  nested delegations (the mutual recursion with
  :func:`settle_dynamic_agent_delegations`), assembling the completed/failed
  handoff rows + parent-resume rows and their live parts.
* :func:`settle_dynamic_agent_delegations` drives the agent-routed loop: each
  round reads the parent's typed ``next_expert`` route, writes the orchestrator
  route into ARC, dispatches the child via :func:`execute_delegated_experts`,
  then re-invokes the parent with the child's returned evidence until it emits
  ``finish`` (or a child cannot run). It returns ``(latest_pred, all_rows)`` --
  the body mirrors today's ``state.pred, state.expert_handoffs = ...`` exactly.

The two ARC writers (:func:`_arc_write_terminal_expert` /
:func:`_arc_write_orchestrator_route`) are relocated here verbatim: they are the
settle engine's private capture helpers, called only from the functions above.

The refactor is byte-for-byte behavior-preserving. ``run_dynamic_agent_sync``
resolves the blueprint-runner seam through ``app`` via a *function-local* import
(the #714 danger-set idiom) so the ``app._blueprint_runner_for_agent`` test
monkeypatch keeps intercepting with zero test edits. The no-progress watchdog
(:func:`~clio_agent.gact.turn_watchdog.await_turn_work` /
:func:`~clio_agent.gact.turn_watchdog.cancel_requested`) is driven off ``state``
via those free functions (Slice 5 extracted ``turn_watchdog.py``).
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact._params import _user_agent_bool_param, _user_agent_int_param
from clio_agent.gact.agents.resolution import (
    _agent_definition_uses_blueprint_runtime,
    _resolve_runtime_dynamic_agent,
    _runtime_declared_child_ids,
)
from clio_agent.gact.delegation import (
    _append_accumulated_workflow_state_context,
    _append_session_workflow_state_context,
    _bubbled_child_evidence_output_summary,
    _clean_public_transcript_text,
    _coerce_expert_handoff_rows,
    _delegated_expert_agent_id,
    _delegated_expert_prompt,
    _dynamic_parent_resume_prompt,
    _failed_child_delegation_output_summary,
    _failed_child_delegation_workflow_state,
    _latest_delegation_output_summary,
    _latest_parent_resumed_output_summary,
    _looks_like_structured_answer,
    _prediction_workflow_state,
    _render_return_summary,
    _should_execute_delegated_handoff,
    _workflow_state_from_handoff_rows,
    _workflow_state_from_outputs,
)
from clio_agent.gact.events import _publish_transcript_event
from clio_agent.gact.evidence import _tool_agent_empty_answer_fallback
from clio_agent.gact.messaging import _prediction_summary
from clio_agent.gact.runtime.globals import (
    _emit_semantic_event,
    _tool_session_context,
    _TurnCancelled,
    _TurnTimedOut,
)
from clio_agent.gact.runtime.type_parsing import _blueprint_module_kind
from clio_agent.gact.streaming import _extract_tools_called, _run_dynamic_agent_compat
from clio_agent.gact.tool_observer import (
    _append_live_assistant_part,
    _handoff_part_metadata,
    _merge_tool_call_rows,
    _sanitize_handoff_tool_metadata,
    _sanitize_tools_called_metadata,
)
from clio_agent.gact.turn_watchdog import await_turn_work, cancel_requested
from clio_agent.gact.types import Part
from clio_agent.gact.workflow_state.merge import _merge_workflow_state_mapping
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.turn_state import TurnState
    from clio_agent.gact.types import AgentDef  # noqa: F401


def _arc_write_terminal_expert(
    app: "FastAPI",
    sid: str,
    scope: str,
    pred: Any,
    turn_id: str,
) -> None:
    """WS2: append a TERMINAL CoT/predict expert's reasoning (``thought``) + final
    answer (``answer`` kind) to its OWN ARC scope.

    ReAct leaves already stream their own thought/tool_call/observation, and a
    *delegating* orchestrator's per-round reasoning + route is written by
    :func:`_arc_write_orchestrator_route` from the settle loop. The remaining gap is
    the expert that produces an answer WITHOUT delegating and WITHOUT a tool loop --
    the synthesis stage, and every orchestrator's own ``finish`` round. Without this,
    those scopes stay empty in ARC even though the answer reached the wire (Principle
    1: ARC must hold the complete trajectory). Best-effort; never breaks a turn."""

    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is None or not sid or not scope:
        return
    reasoning = str(getattr(pred, "reasoning", "") or "").strip()
    answer = str(getattr(pred, "answer", "") or "").strip()
    if not reasoning and not answer:
        return
    expert_span_id = f"{turn_id}:{scope}" if turn_id else scope
    try:
        step = 0
        if reasoning:
            arc.append_segment(
                sid,
                scope,
                "thought",
                {"text": reasoning},
                step=step,
                token_count=max(1, len(reasoning) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
            step += 1
        if answer:
            # The deliverable rides the dedicated ``answer`` kind (an expert/turn final
            # message) -- substrate-complete but outside the working-set/prompt render,
            # so it never re-enters a downstream prompt.
            arc.append_segment(
                sid,
                scope,
                "answer",
                {"text": answer},
                step=step,
                token_count=max(1, len(answer) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
    except Exception:  # noqa: BLE001 - ARC capture is best-effort, never break a turn
        if trace.HF_ON:
            trace.hot("ARC-TERMINAL-WRITE-FAIL", "%s", scope)


def _arc_write_orchestrator_route(
    app: "FastAPI",
    sid: str,
    scope: str,
    pred: Any,
    next_expert: str,
    next_task: str,
    turn_id: str,
) -> None:
    """WS2: append an orchestrator's reasoning (thought) + delegation (tool_call) to
    its OWN ARC scope, so a predict/CoT orchestrator's working-set trajectory is no
    longer empty (the ReAct leaves already cover themselves). Best-effort; never
    breaks a turn."""

    arc = getattr(getattr(app, "state", None), "arc", None)
    if arc is None or not sid or not scope:
        return
    expert_span_id = f"{turn_id}:{scope}" if turn_id else scope
    try:
        import json as _json  # noqa: PLC0415

        step = 0
        reasoning = str(getattr(pred, "reasoning", "") or "").strip()
        if reasoning:
            arc.append_segment(
                sid,
                scope,
                "thought",
                {"text": reasoning},
                step=step,
                token_count=max(1, len(reasoning) // 4),
                turn_id=turn_id,
                expert_span_id=expert_span_id,
            )
            step += 1
        # A delegation IS a call to a child expert (the ReAct path already models
        # children as tools), so it rides the ``tool_call`` kind.
        delegation = {"name": next_expert, "args": {"task": next_task}}
        arc.append_segment(
            sid,
            scope,
            "tool_call",
            delegation,
            step=step,
            token_count=max(1, len(_json.dumps(delegation, default=str)) // 4),
            turn_id=turn_id,
            expert_span_id=expert_span_id,
        )
    except Exception:  # noqa: BLE001 - ARC capture is best-effort, never break a turn
        if trace.HF_ON:
            trace.hot("ARC-ROUTE-WRITE-FAIL", "%s", scope)


async def run_dynamic_agent_sync(
    state: "TurnState", agent_def: "AgentDef", prompt: str
) -> Any:
    # #714 danger set: the blueprint-runner seam is resolved through ``app``
    # via a function-local import so the ``app._blueprint_runner_for_agent``
    # test monkeypatch keeps intercepting with zero test edits.
    from clio_agent.gact.app import _blueprint_runner_for_agent  # noqa: PLC0415

    runner = _blueprint_runner_for_agent(agent_def)
    loop = asyncio.get_running_loop()
    with _tool_session_context(state.sid):
        # The signature is rebuilt inside the executor (via _build_blueprint_dspy_module);
        # its routing Literal[children, "finish"] resolves children from the active
        # blueprint keyed on _ACTIVE_GACT_SESSION_ID. Set it here so the copied context
        # carries it -- otherwise children resolve empty and next_expert collapses to
        # Literal["finish"], forcing the agent to finish immediately. The keystone
        # (set_turn_identity) already binds active_app() for the whole turn, so no
        # _gact_app_context wrapper is needed here.
        _sid_tok = _ctx.set_session_id(state.sid)
        try:
            turn_context = contextvars.copy_context()
        finally:
            _ctx.reset(_sid_tok)
    _pred = await await_turn_work(
        state,
        loop.run_in_executor(
            None,
            lambda: turn_context.run(
                _run_dynamic_agent_compat,
                runner,
                state.app.state.agent,
                agent_def,
                prompt,
                state.sid,
                partial(cancel_requested, state),
            ),
        ),
    )
    # RAW-ROUTE instrumentation: what did THIS agent's LM actually emit as
    # structured expert_handoffs (before any continuation-contract injection)?
    # Distinguishes agent-driven routing (model emits handoffs) from
    # contract-driven routing (model emits none; the when_child_completed state
    # machine injects the next expert). Answers whether we can move to the
    # minimal agent-routed loop or whether the blueprint must teach routing.
    trace.route(
        "RAW-ROUTE",
        "agent=%s next_expert=%s next_task_len=%d answer_len=%d",
        getattr(agent_def, "id", "?"),
        str(getattr(_pred, "next_expert", "") or "") or "<none>",
        len(str(getattr(_pred, "next_task", "") or "")),
        len(str(getattr(_pred, "answer", "") or "")),
    )
    trace.HF_ON and trace.hot(
        "TURN-SEMANTICS",
        "agent=%s reasoning=%r answer=%r next_expert=%r next_task=%r workflow_state_type=%s",
        getattr(agent_def, "id", "?"),
        str(getattr(_pred, "reasoning", "") or "")[:500],
        str(getattr(_pred, "answer", "") or "")[:300],
        str(getattr(_pred, "next_expert", "") or ""),
        str(getattr(_pred, "next_task", "") or "")[:300],
        type(getattr(_pred, "workflow_state", None)).__name__,
    )
    # Per-expert capture: one expert.response.completed per dynamic-agent run
    # (child or parent-resume), carrying that expert's full reasoning +
    # trajectory via _prediction_summary. Closes the nested-expert capture
    # gap -- each expert's own LM output is recorded under the turn's trace,
    # correlated by actor agent_id and the parent_expert in blueprint.
    _agent_meta = getattr(agent_def, "metadata", {}) or {}
    _emit_semantic_event(
        state.app,
        state.sid,
        "expert.response.completed",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        summary=f"Expert {getattr(agent_def, 'id', '?')} produced a response.",
        actor={"agent_id": str(getattr(agent_def, "id", "") or "")},
        blueprint={
            "agent_blueprint_id": str(_agent_meta.get("agent_blueprint_id") or ""),
            "parent_expert": str(getattr(agent_def, "parent_id", "") or ""),
        },
        provider={
            "provider_id": str(getattr(agent_def, "default_provider", "") or ""),
            "model_id": str(getattr(agent_def, "default_model", "") or ""),
        },
        payload=_prediction_summary(_pred),
    )
    _workflow_state = _prediction_workflow_state(_pred)
    if _workflow_state:
        _publish_transcript_event(
            state.bus,
            state.sid,
            "state.updated",
            {
                "turn_id": state.turn_id,
                "value": _workflow_state,
                "visibility": "hidden",
            },
        )
    # #733 (#767 PR3, mechanism 4's replacement): a TERMINAL expert's answer
    # settles its exactly-once (agent, "answer") channel HERE, at the LM-call
    # site — when the answer already streamed live the fallback is audited +
    # ignored by op identity; when it did not stream, ONE batch added+completed
    # burst lands now, in arrival order. This replaces the
    # ``expert_terminal_answers`` side-ledger + finalize ``answered_agents``
    # scan. The TURN-level responder's own terminal answer is deliberately
    # left to finalize's canonical-answer channel (its fallback text is
    # grounded against on-disk artifacts there). Delegating rounds settle
    # nothing (the deliverable is the delegation, not an answer); structured
    # (JSON) answers stay out of the visible transcript, as before.
    _expert_id = str(getattr(agent_def, "id", "") or "")
    _answer_text = str(getattr(_pred, "answer", "") or "").strip()
    _next_expert = str(getattr(_pred, "next_expert", "") or "").strip()
    _is_terminal = _next_expert not in _runtime_declared_child_ids(
        state.app, _expert_id, session_id=state.sid
    )
    if (
        _answer_text
        and _is_terminal
        and _expert_id
        and _expert_id != state.invocation_agent_id
        and not _looks_like_structured_answer(_answer_text)
    ):
        state.transcript.field_stream(_expert_id, "answer").finish(fallback_text=_answer_text)
    # WS2: capture a TERMINAL CoT/predict expert's reasoning + answer in its own
    # ARC scope. Delegating rounds are written by the settle loop's
    # _arc_write_orchestrator_route (route per round); ReAct leaves self-write.
    # The remaining gap -- synthesis and every expert's `finish` round (an answer,
    # no delegation, no tool loop) -- is filled here, where `app` is reliably bound
    # and `_pred` carries the full reasoning + answer.
    if _agent_definition_uses_blueprint_runtime(agent_def):
        _kind = _blueprint_module_kind(agent_def)
        if _kind and _kind != "react":
            _next = str(getattr(_pred, "next_expert", "") or "").strip()
            _declared = _runtime_declared_child_ids(state.app, agent_def.id, session_id=state.sid)
            if _next not in _declared:  # terminal round (finish/empty/non-route)
                _arc_write_terminal_expert(state.app, state.sid, agent_def.id, _pred, state.turn_id)
    return _pred

async def execute_delegated_experts(
    state: "TurnState",
    parent_agent: "AgentDef",
    rows: list[dict[str, Any]],
    *,
    source_text: str,
    completed_child_ids: set[str] | None = None,
    completed_child_outputs: dict[str, str] | None = None,
    depth: int = 0,
    seen: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    if seen is None:
        seen = {parent_agent.id}
    completed_child_ids = completed_child_ids or set()
    completed_child_outputs = completed_child_outputs or {}
    if depth >= 3:
        return [
            {
                **row,
                "status": "skipped",
                "skip_reason": "max_delegate_depth_reached",
                "parent_id": parent_agent.id,
                "depth": depth,
            }
            for row in rows
            if _should_execute_delegated_handoff(row)
        ]

    executed: list[dict[str, Any]] = []
    for row in rows:
        if not _should_execute_delegated_handoff(row):
            executed.append(row)
            continue
        target_id = _delegated_expert_agent_id(row)
        if not target_id:
            executed.append(
                {
                    **row,
                    "status": "skipped",
                    "skip_reason": "missing_delegate_target",
                    "parent_id": parent_agent.id,
                    "depth": depth,
                }
            )
            continue
        target = _resolve_runtime_dynamic_agent(state.app, target_id, session_id=state.sid)
        if target is None or target.source != "expert_pack" or not target.enabled:
            executed.append(
                {
                    **row,
                    "agent_id": target_id,
                    "status": "failed",
                    "error": "delegate_not_available",
                    "parent_id": parent_agent.id,
                    "depth": depth,
                }
            )
            continue
        if target.parent_id != parent_agent.id:
            executed.append(
                {
                    **row,
                    "agent_id": target_id,
                    "status": "failed",
                    "error": "delegate_parent_mismatch",
                    "parent_id": parent_agent.id,
                    "target_parent_id": target.parent_id,
                    "depth": depth,
                }
            )
            continue
        if target.id in seen:
            executed.append(
                {
                    **row,
                    "agent_id": target_id,
                    "status": "failed",
                    "error": "delegate_cycle_detected",
                    "parent_id": parent_agent.id,
                    "depth": depth,
                }
            )
            continue

        prompt = _append_session_workflow_state_context(
            state.app,
            state.sid,
            _delegated_expert_prompt(row, source_text),
        )
        target_kind = (
            _blueprint_module_kind(target)
            if _agent_definition_uses_blueprint_runtime(target)
            else ""
        )
        execution_mode = (
            f"blueprint_{target_kind}"
            if target_kind
            else ("tool_agent" if target.tools else "prompt_agent")
        )
        is_blueprint_delegation = bool(target_kind)
        delegation_event_prefix = (
            "blueprint.delegation" if is_blueprint_delegation else "delegation"
        )
        delegation_blueprint = {
            "pack_id": str(target.metadata.get("pack_id") or ""),
            "pack_version": str(target.metadata.get("pack_version") or ""),
            "agent_blueprint_id": str(target.metadata.get("agent_blueprint_id") or ""),
            "parent_expert": parent_agent.id,
            "child_expert": target.id,
        }
        started_at = time.perf_counter()
        started_row = {
            **row,
            "agent_id": target.id,
            "parent_id": parent_agent.id,
            "pack_id": str(target.metadata.get("pack_id") or ""),
            "pack_version": str(target.metadata.get("pack_version") or ""),
            "status": "running",
            "stage": "delegate.started",
            "delegation_lifecycle": "sync",
            "depth": depth,
            "execution_mode": execution_mode,
        }
        _emit_semantic_event(
            state.app,
            state.sid,
            f"{delegation_event_prefix}.started",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="running",
            summary=f"{parent_agent.id} delegated sync work to {target.id}.",
            actor={"agent_id": parent_agent.id, "role": "parent_expert"},
            subject={"agent_id": target.id, "role": "child_expert"},
            blueprint=delegation_blueprint,
            provider={
                "provider_id": target.default_provider,
                "model_id": target.default_model,
            },
            payload=started_row,
        )
        _append_live_assistant_part(
            state.app,
            state.sid,
            Part(
                id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                type="expert_handoff",
                agent_id=parent_agent.id,
                parent_agent=parent_agent.id,
                child_agent=target.id,
                stage=str(started_row.get("stage") or ""),
                status=str(started_row.get("status") or ""),
                # The orchestrator's reasoning rides the delegation atom (#732).
                thought=str(started_row.get("thought") or ""),
                text=f"{parent_agent.id} -> {target.id}",
                metadata={**_handoff_part_metadata(started_row), "stream_source": "live"},
            ),
        )
        ledger_start = 0
        ledger = getattr(state.app.state, "tool_call_ledger", None)
        if isinstance(ledger, dict):
            session_rows = ledger.get(state.sid)
            if isinstance(session_rows, list):
                ledger_start = len(session_rows)
        try:
            pred_child = await run_dynamic_agent_sync(state, target, prompt)
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            local_tools_called = _extract_tools_called(pred_child)
            # Seed local state from the child's typed workflow_state output
            # field (structural twin of the removed prose append), then merge
            # any tool-row state. The state rides the completed_row's
            # ``workflow_state`` Mapping below; it is never serialized into the
            # output text.
            local_workflow_state = _prediction_workflow_state(pred_child)
            for tool_row in local_tools_called:
                row_state = tool_row.get("workflow_state")
                if isinstance(row_state, Mapping):
                    _merge_workflow_state_mapping(local_workflow_state, row_state)
            nested: list[dict[str, Any]] = []
            if target.source == "expert_pack":
                pred_child, nested = await settle_dynamic_agent_delegations(
                    state,
                    target,
                    pred_child,
                    source_text=prompt,
                )
            raw_answer = str(getattr(pred_child, "answer", "") or "").strip()
            # The child's typed workflow_state is carried structurally on the
            # completed_row below (NOT serialized into output text).
            output = raw_answer
            if not nested:
                child_rows = _coerce_expert_handoff_rows(
                    getattr(pred_child, "expert_handoffs", None)
                )
                nested = await execute_delegated_experts(
                    state,
                    target,
                    child_rows,
                    source_text=prompt,
                    completed_child_ids=completed_child_ids,
                    completed_child_outputs=completed_child_outputs,
                    depth=depth + 1,
                    seen={*seen, target.id},
                )
            # STRUCTURAL empty-answer fallback (no prose-keyword scanning): a
            # tool-driven child can produce real tool evidence but an EMPTY prose
            # answer. ONLY when the answer is genuinely empty do we surface the
            # tool-trajectory evidence or the latest nested-child summary -- a
            # non-empty answer IS the agent's deliverable and is left untouched.
            if not raw_answer:
                fallback = (
                    _tool_agent_empty_answer_fallback(getattr(pred_child, "trajectory", None))
                    or _latest_parent_resumed_output_summary(nested, target.id)
                    or _latest_delegation_output_summary(nested)
                )
                if fallback:
                    output = fallback
            if nested and (
                _user_agent_bool_param(
                    target,
                    "bubble_child_evidence_on_completion",
                )
                or _user_agent_bool_param(
                    target,
                    "return_child_evidence_on_completion",
                )
            ):
                declared_target_child_ids = _runtime_declared_child_ids(
                    state.app,
                    target.id,
                    session_id=state.sid,
                )
                output = (
                    _bubbled_child_evidence_output_summary(
                        nested,
                        target.id,
                        declared_target_child_ids,
                    )
                    or output
                )
            # Nested child typed state is merged into the structured
            # ``workflow_state`` carrier below (via _workflow_state_from_handoff_rows);
            # it is NOT appended to output text anymore.
            child_tools_called = _extract_tools_called(pred_child)
            if isinstance(ledger, dict):
                session_rows = ledger.get(state.sid)
                if isinstance(session_rows, list) and len(session_rows) > ledger_start:
                    child_tools_called = _merge_tool_call_rows(
                        child_tools_called,
                        [
                            dict(row)
                            for row in session_rows[ledger_start:]
                            if isinstance(row, Mapping)
                        ],
                    )
            # The prompt may carry prior accumulated typed state (injected via
            # _append_accumulated_workflow_state_context); parse it so prior
            # state still bubbles. The child's OWN typed state comes from its
            # structured workflow_state field via local_workflow_state (the
            # structural twin of the removed prose append) -- never re-parsed
            # out of `output` text, which no longer carries a state block.
            workflow_state = _workflow_state_from_outputs([prompt])
            # Seed from this expert's own authoritative typed emission so its
            # state bubbles to the parent for continuation routing, even when
            # `output` was reassigned to a child-evidence summary. Generic for
            # all packs.
            if local_workflow_state:
                _merge_workflow_state_mapping(workflow_state, local_workflow_state)
            if nested:
                _merge_workflow_state_mapping(
                    workflow_state,
                    _workflow_state_from_handoff_rows(nested),
                )
            for tool_row in child_tools_called:
                row_state = tool_row.get("workflow_state")
                if isinstance(row_state, Mapping):
                    _merge_workflow_state_mapping(workflow_state, row_state)
            child_tools_called = _sanitize_tools_called_metadata(child_tools_called)
            handoff_output = "" if _looks_like_structured_answer(output) else output
            # The PUBLIC return summary is derived from the child's GENUINE
            # answer (structured answers rendered to a readable one-liner) so
            # the transcript shows the real result, not a generic placeholder.
            # handoff_output stays blanked for structured answers because it
            # feeds the parent RESUME PROMPT (which receives the typed
            # workflow_state separately, not raw JSON).
            public_return_summary = (
                _clean_public_transcript_text(_render_return_summary(output))
                or f"{target.id} returned to {parent_agent.id}."
            )
            completed_row = {
                **row,
                "agent_id": target.id,
                "parent_id": parent_agent.id,
                "pack_id": str(target.metadata.get("pack_id") or ""),
                "pack_version": str(target.metadata.get("pack_version") or ""),
                "provider_id": target.default_provider,
                "model_id": target.default_model,
                "fallback_warnings": list(target.validation_errors),
                "status": "completed",
                "stage": "delegate.completed",
                "delegation_lifecycle": "sync",
                "return_to": parent_agent.id,
                "depth": depth,
                "duration_ms": duration_ms,
                "execution_mode": execution_mode,
                "input": prompt,
                "output": handoff_output,
                # Real, human-readable return summary — the same string the
                # live delegation-return render shows, so the reload (/messages)
                # render matches the live render (no change-on-reload).
                "output_summary": public_return_summary,
                # The GENUINE structured output for the "details" disclosure on
                # reload (mirrors the live return's `response`). Empty for prose
                # (the body already is the answer). Distinct from `output`, which
                # stays blanked to keep the parent resume prompt clean.
                "output_raw": output if _looks_like_structured_answer(output) else "",
                "workflow_state": workflow_state,
                "tools_called": child_tools_called,
                "children": [
                    _sanitize_handoff_tool_metadata(child)
                    if isinstance(child, Mapping)
                    else child
                    for child in nested
                ],
            }
            # completed_row carries the child's GENUINE output (the typed
            # dspy.extract deliverable) verbatim — no heuristic compaction.
            # It flows to the parent resume prompt + live Part; capture
            # (trace/ARC) carries the same full output.
            _emit_semantic_event(
                state.app,
                state.sid,
                f"{delegation_event_prefix}.completed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                summary=public_return_summary,
                actor={"agent_id": target.id, "role": "child_expert"},
                subject={"agent_id": parent_agent.id, "role": "parent_expert"},
                blueprint=delegation_blueprint,
                provider={
                    "provider_id": target.default_provider,
                    "model_id": target.default_model,
                },
                payload=dict(completed_row),
            )
            _append_live_assistant_part(
                state.app,
                state.sid,
                Part(
                    id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                    type="expert_handoff",
                    agent_id=parent_agent.id,
                    parent_agent=parent_agent.id,
                    child_agent=target.id,
                    stage=str(completed_row.get("stage") or ""),
                    status=str(completed_row.get("status") or ""),
                    text=f"{parent_agent.id} <- {target.id}",
                    metadata={
                        **_handoff_part_metadata(completed_row),
                        "stream_source": "live",
                    },
                ),
            )
            if workflow_state:
                _publish_transcript_event(
                    state.bus,
                    state.sid,
                    "state.updated",
                    {
                        "turn_id": state.turn_id,
                        "value": workflow_state,
                        "visibility": "hidden",
                    },
                )
            completed_row = _sanitize_handoff_tool_metadata(completed_row)
            executed.append(completed_row)
            resumed_row = {
                "agent_id": parent_agent.id,
                "parent_id": parent_agent.parent_id,
                "dispatch_target": parent_agent.id,
                "status": "completed",
                "stage": "parent.resumed",
                "delegation_lifecycle": "sync",
                "resumed_from": target.id,
                "depth": depth,
                "output": handoff_output,
                "workflow_state": workflow_state,
            }
            _emit_semantic_event(
                state.app,
                state.sid,
                f"{delegation_event_prefix}.parent_resumed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                summary=f"{parent_agent.id} resumed after {target.id}.",
                actor={"agent_id": parent_agent.id, "role": "parent_expert"},
                subject={"agent_id": target.id, "role": "child_expert"},
                blueprint=delegation_blueprint,
                payload=resumed_row,
            )
            _append_live_assistant_part(
                state.app,
                state.sid,
                Part(
                    id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                    type="expert_handoff",
                    agent_id=parent_agent.id,
                    parent_agent=str(parent_agent.parent_id or ""),
                    child_agent=parent_agent.id,
                    stage=str(resumed_row.get("stage") or ""),
                    status=str(resumed_row.get("status") or ""),
                    text=f"{parent_agent.id} resumed (from {target.id})",
                    metadata={
                        **_handoff_part_metadata(resumed_row),
                        "stream_source": "live",
                    },
                ),
            )
            executed.append(resumed_row)
        except (_TurnCancelled, _TurnTimedOut):
            raise
        except Exception as exc:  # noqa: BLE001
            child_tools_called = []
            if isinstance(ledger, dict):
                session_rows = ledger.get(state.sid)
                if isinstance(session_rows, list) and len(session_rows) > ledger_start:
                    child_tools_called = _merge_tool_call_rows(
                        child_tools_called,
                        [
                            dict(row)
                            for row in session_rows[ledger_start:]
                            if isinstance(row, Mapping)
                        ],
                    )
            error_name = type(exc).__name__
            error_message = str(exc)
            workflow_state = _failed_child_delegation_workflow_state(
                prompt=prompt,
                child_agent_id=target.id,
                parent_agent_id=parent_agent.id,
                error=error_name,
                message=error_message,
                tools_called=child_tools_called,
            )
            output = _failed_child_delegation_output_summary(
                child_agent_id=target.id,
                parent_agent_id=parent_agent.id,
                error=error_name,
                message=error_message,
            )
            child_tools_called = _sanitize_tools_called_metadata(child_tools_called)
            failed_row = {
                **row,
                "agent_id": target.id,
                "parent_id": parent_agent.id,
                "pack_id": str(target.metadata.get("pack_id") or ""),
                "pack_version": str(target.metadata.get("pack_version") or ""),
                "provider_id": target.default_provider,
                "model_id": target.default_model,
                "fallback_warnings": list(target.validation_errors),
                "status": "failed",
                "stage": "delegate.failed",
                "depth": depth,
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "execution_mode": execution_mode,
                "error": error_name,
                "message": error_message,
                "output": output,
                "workflow_state": workflow_state,
                "tools_called": child_tools_called,
            }
            _emit_semantic_event(
                state.app,
                state.sid,
                f"{delegation_event_prefix}.failed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="failed",
                summary=f"{target.id} failed during sync delegation.",
                actor={"agent_id": target.id, "role": "child_expert"},
                subject={"agent_id": parent_agent.id, "role": "parent_expert"},
                blueprint=delegation_blueprint,
                provider={
                    "provider_id": target.default_provider,
                    "model_id": target.default_model,
                },
                payload=_sanitize_handoff_tool_metadata(failed_row),
            )
            _append_live_assistant_part(
                state.app,
                state.sid,
                Part(
                    id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                    type="expert_handoff",
                    agent_id=parent_agent.id,
                    parent_agent=parent_agent.id,
                    child_agent=target.id,
                    stage=str(failed_row.get("stage") or ""),
                    status=str(failed_row.get("status") or ""),
                    text=f"{parent_agent.id} -> {target.id} (failed)",
                    metadata={
                        **_handoff_part_metadata(failed_row),
                        "stream_source": "live",
                    },
                ),
            )
            executed.append(_sanitize_handoff_tool_metadata(failed_row))
    return executed

async def settle_dynamic_agent_delegations(
    state: "TurnState",
    parent_agent: "AgentDef",
    initial_pred: Any,
    *,
    source_text: str,
) -> tuple[Any, list[dict[str, Any]]]:
    """Run sync child delegations and re-enter the parent with compact returns."""

    latest_pred = initial_pred
    if trace.ROUTE_ON:
        import traceback as _tb_enter  # noqa: PLC0415

        _clio_frames = [
            ln.strip().split("\n")[0] for ln in _tb_enter.format_stack() if "gact/app.py" in ln
        ]
        trace.route(
            "SETTLE-ENTER",
            "parent=%s caller=%s",
            parent_agent.id,
            " <- ".join(reversed(_clio_frames[-6:])),
        )
    all_rows: list[dict[str, Any]] = []
    max_rounds = _user_agent_int_param(parent_agent, "max_sync_delegation_rounds", 12)
    max_rounds = max(1, min(max_rounds, 16))
    completed_child_ids: set[str] = set()
    completed_child_outputs: dict[str, str] = {}
    declared_child_ids = _runtime_declared_child_ids(state.app, parent_agent.id, session_id=state.sid)

    for _round in range(max_rounds):
        # AGENT-DRIVEN ROUTING. The parent emitted, at the end of its run, a typed
        # ``next_expert``: the ONE child to descend into, or "finish" to return to
        # ITS parent. No contracts, no prose heuristics -- the structured field IS
        # the routing decision (built in _blueprint_runtime_signature as
        # Literal[children + "finish"]). "finish"/missing/unknown-id => this expert
        # is done; finalize with its ``answer``. main's parent is the user, so main's
        # answer on "finish" is the deliverable.
        next_expert = str(getattr(latest_pred, "next_expert", "") or "").strip()
        next_task = str(getattr(latest_pred, "next_task", "") or "").strip()
        if trace.ROUTE_ON:
            trace.route(
                "SETTLE",
                "parent=%s round=%d completed=%s next_expert=%s next_task=%r reasoning=%r answer=%r",
                parent_agent.id,
                _round,
                sorted(completed_child_ids),
                next_expert or "<none>",
                next_task[:160],
                str(getattr(latest_pred, "reasoning", "") or "")[:400],
                str(getattr(latest_pred, "answer", "") or "")[:200],
            )
        if (
            next_expert in ("", "finish", "none", "done", "stop")
            or next_expert not in declared_child_ids
        ):
            break
        # WS2: stream this orchestrator's routing decision (its reasoning + the
        # delegation) into its OWN ARC scope so ARC internally holds the complete
        # spine -- the ReAct *leaves* already write thought/tool_call/observation,
        # but the predict/CoT orchestrators (main/data/analysis/synthesis) wrote
        # nothing to their scope, so it was empty. Captured here on the MAIN loop
        # (where ``app.state.arc`` is reliably bound and ``latest_pred`` carries the
        # reasoning), not in the streamed executor forward (no app contextvar).
        _arc_write_orchestrator_route(
            state.app, state.sid, parent_agent.id, latest_pred, next_expert, next_task, state.turn_id
        )
        requested_rows = [
            {
                "delegate_to": next_expert,
                "agent_id": next_expert,
                "question": next_task or source_text,
                # The orchestrator's reasoning that produced THIS delegation —
                # carried onto the delegate.started handoff part as its thought
                # so a delegation turn renders as text + the delegation, one
                # ordered event, just like a tool turn (#732).
                "thought": str(getattr(latest_pred, "reasoning", "") or ""),
                "status": "requested",
                "execute": True,
                "source": "agent_next_expert",
            }
        ]
        # Forward the parent's CURRENT accumulated state (the typed workflow_state it
        # is holding right now, e.g. station_catalog.station_ids produced by an
        # earlier sibling) as the child's parent-evidence. The static `source_text`
        # is the parent's ORIGINAL input, captured before earlier children ran -- so a
        # later child (e.g. the resolver) otherwise never sees the ranked list it is
        # documented to consume, and falls back to inventing candidate ids.
        # State travels STRUCTURALLY: read the parent's typed workflow_state field
        # and inject it via the clean structured prompt formatter, rather than
        # appending a prose state block to the parent's answer.
        current_evidence = str(getattr(latest_pred, "answer", "") or "").strip()
        parent_state = _prediction_workflow_state(latest_pred)
        if parent_state:
            current_evidence = _append_accumulated_workflow_state_context(
                current_evidence, parent_state
            ).strip()
        executed_rows = await execute_delegated_experts(
            state,
            parent_agent,
            requested_rows,
            source_text=current_evidence or source_text,
            completed_child_ids=completed_child_ids,
            completed_child_outputs=completed_child_outputs,
        )
        all_rows.extend(executed_rows)
        completed_this_round = [
            row
            for row in executed_rows
            if row.get("status") == "completed" and row.get("stage") == "delegate.completed"
        ]
        for row in completed_this_round:
            cid = str(row.get("agent_id") or row.get("delegate_to") or "").strip()
            if cid:
                completed_child_ids.add(cid)
                completed_child_outputs[cid] = str(
                    row.get("output") or row.get("output_summary") or row.get("summary") or ""
                ).strip()
        if not completed_this_round:
            # Child could not run (unavailable / cycle / error). Stop instead of
            # looping; the parent's current answer carries whatever evidence exists.
            break
        # Re-invoke the parent with the child's returned evidence so IT emits the
        # next route (descend again, or finish).
        resume_prompt = _dynamic_parent_resume_prompt(
            source_text, parent_agent, all_rows, declared_child_ids=declared_child_ids
        )
        latest_pred = await run_dynamic_agent_sync(state, parent_agent, resume_prompt)

    # The genuine final answer flows to the parent verbatim; the heuristic
    # evidence-scaffolding scrubber has been removed.
    return latest_pred, all_rows
