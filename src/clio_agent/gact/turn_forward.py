"""Forward-orchestration seam for the GACT turn engine (#767 Phase B).

:func:`forward_turn` resolves the turn's agent (the session's pinned plugin
resolution, or a per-message override resolved for this turn), builds its DSPy
module, and runs the agent's flow ONCE in the session's exclusive worker (live
text streams through the LM token hooks). It sets ``state.active_agent_id`` /
``state.invocation_agent_id`` / ``state.agent_runtime`` /
``state.prompt_resolution`` / ``state.dynamic_agent_used`` / ``state.pred`` and
returns ``state.pred``. A react agent delegates through the spawn tools its
plugins provide and the shared ``wait_tasks`` controls, so ``state.pred`` is
already the agent's own answer.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, Callable

from clio_agent.gact import context as _ctx
from clio_agent.gact import session_warmup
from clio_agent.gact.agents.prompt_basis import (
    turn_agent_def,
    unlisted_tool_names,
    with_tool_introductions,
)
from clio_agent.gact.agents.resolution import (
    apply_session_tool_allowlist,
    resolution_agent_def,
    resolve_for_session,
    session_resolution,
)
from clio_agent.gact.evidence import _dynamic_agent_runtime_provenance
from clio_agent.gact.messaging import _prediction_summary
from clio_agent.gact.native_model_inputs import native_input_kwargs
from clio_agent.gact.off_loop import emit_semantic_event_async
from clio_agent.gact.providers.auth import _refresh_argonne_lm_token
from clio_agent.gact.runtime import bringup_timing
from clio_agent.gact.runtime.globals import (
    _async_tool_session_context,
    _llm_provider_payload,
    _session_agent_id,
    _tool_session_context,
)
from clio_agent.gact.turn_reasoning import apply_turn_reasoning, record_turn_reasoning
from clio_agent.gact.turn_watchdog import await_turn_work, cancel_requested

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState
    from clio_agent.gact.types import AgentDef


async def _run_turn_setup_off_loop(state: "TurnState", function: Callable[[], Any]) -> Any:
    """Run finite preparation off-loop and retire it before propagating cancellation."""
    from clio_agent.gact.off_loop import join_off_loop

    return await join_off_loop(function)


async def _run_module(state: "TurnState", module: Any, cancel: Callable[[], bool]) -> Any:
    """Run the production module in this session's exclusively owned turn worker."""
    from clio_agent.agent import cancellation_checker

    def forward() -> Any:
        with cancellation_checker(cancel), _tool_session_context(state.sid, lease_fleet=False):
            from clio_agent.errors import CancellationError

            if cancel():
                raise CancellationError("Session stopped before startup")
            with state.app.state.task_history.serialized():
                assignment = state.app.state.task_history.latest_assignment(state.sid)
                if assignment is None:
                    raise ValueError("Session assignment is unavailable before initialization")
                state.app.state.conversations.require_admission(assignment)
            # Hooks run on this session's existing worker. They never hold the
            # shared task writer, and Stop waits for their physical retirement.
            state.app.state.sessions.initialize(state.sid, cancel_event=state.turn_cancel_event)
            state.app.state.conversations.note_startup_interruption(state.sid)
            if cancel():
                raise CancellationError("Session stopped during startup")
            with state.app.state.task_history.serialized():
                state.app.state.conversations.require_admission(assignment)
            kwargs = {
                "question": state.enriched_text,
                "session_id": state.sid,
                "session_mode": state.sess.mode,
                "session_edit_mode": state.sess.edit_mode,
                "cancel_requested": cancel,
                **native_input_kwargs(
                    state.app,
                    state.sid,
                    module,
                    images=state.native_images,
                    files=state.native_files,
                ),
            }
            injection_token = _ctx.set_turn_injections(state.injections)
            try:
                return module(**kwargs)
            finally:
                _ctx.reset(injection_token)

    return await state.app.state.turn_runner.run_sync(state.sid, forward)


async def forward_turn(state: "TurnState") -> Any:
    """Retain the workspace lease for this segment; no whole-turn executor pool."""
    async with _async_tool_session_context(state.sid):
        return await _forward_turn_leased(state)


def _apply_turn_model_selection(state: "TurnState", agent_def: "AgentDef") -> "AgentDef":
    """Overlay an accepted per-message/session route onto this turn's local agent.

    ``message_submission.accept_message`` already validated the selection (it
    executes only when the active LM serves it or the provider catalog holds live
    handshake evidence) and recorded the deciding layer on the user message. This
    copies that decision onto a LOCAL copy of the agent definition, so the routed
    model reaches the forward without mutating the stored agent.
    """

    source = str(state.user_msg.metadata.get("model_selection_source") or "global_active")
    raw = state.user_msg.metadata.get("effective_model")
    role = str((state.sess.metadata or {}).get("model_role") or "")
    if role:  # a server-run session on a fleet role (the memory curator), every segment
        from clio_agent.gact.memory.models import memory_model_identity  # noqa: PLC0415

        raw, source = memory_model_identity(role), "fleet_role"  # type: ignore[arg-type]
    if source == "global_active" or not isinstance(raw, dict):
        return agent_def
    provider_id = str(raw.get("provider_id") or "").strip()
    model_id = str(raw.get("model_id") or "").strip()
    if not provider_id or not model_id:
        return agent_def
    metadata = dict(agent_def.metadata)
    metadata["turn_model_selection_source"] = source
    return agent_def.model_copy(
        update={
            "default_provider": provider_id,
            "default_model": model_id,
            "metadata": metadata,
        }
    )


async def _forward_turn_leased(state: "TurnState") -> Any:
    """Resolve the turn's agent, run its flow and return the prediction.

    The agent is the session's pinned plugin resolution; a per-message agent
    override resolves that agent for this turn only. Sets
    ``state.invocation_agent_id`` / ``state.active_agent_id`` /
    ``state.agent_runtime`` / ``state.prompt_resolution`` /
    ``state.dynamic_agent_used`` and returns ``state.pred``.
    """

    from clio_agent.gact.agents.builders import build_agent_module  # noqa: PLC0415

    cancel_cb = partial(cancel_requested, state)
    session_warmup.release_session_fleet(state.sid)  # the lease below holds it now
    session_warmup.start_session_warmup(state.app, state.sid, trigger="turn_started")
    bringup_timing.timer_for_session(state.app, state.sid).start_phase("agent.resolve")
    session_agent_id = _session_agent_id(state.sess)
    resolution = await _run_turn_setup_off_loop(
        state,
        lambda: (
            resolve_for_session(state.app, state.turn_agent_id)
            if state.turn_agent_id
            else session_resolution(state.app, state.sid)
        ),
    )
    if state.turn_agent_id:
        turn_def, build_resolution = resolution_agent_def(resolution), resolution
    else:
        turn_def, build_resolution = await _run_turn_setup_off_loop(
            state, lambda: turn_agent_def(state.app, state.sid, resolution)
        )
    dynamic_agent = apply_session_tool_allowlist(state.app, turn_def, session_id=state.sid)
    state.active_agent_id = dynamic_agent.id
    _ctx.set_primary_agent_id(state.active_agent_id)
    routing_mode = getattr(state.sess, "routing_mode", "auto") or "auto"
    state.invocation_agent_id = state.active_agent_id
    await emit_semantic_event_async(
        state.app,
        state.sid,
        "agent.invocation.started",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="running",
        summary=f"Invoking {state.invocation_agent_id}.",
        actor={"agent_id": state.invocation_agent_id},
        subject={"message_id": state.user_msg.id},
        payload={
            "routing_mode": routing_mode,
            "session_agent_id": session_agent_id,
            "turn_agent_id": state.turn_agent_id,
            "agent_ref": resolution.agent.get("ref", ""),
            "resolution_digest": resolution.digest,
            "flow": resolution.agent.get("flow", {}),
        },
    )
    from clio_agent.agent import cancellation_checker as _cancellation_checker  # noqa: PLC0415

    _refresh_argonne_lm_token(state.app.state.agent)

    dynamic_agent = _apply_turn_model_selection(state, dynamic_agent)
    # The message's own reasoning effort overrides the configured level for
    # this turn only (a local agent copy; see gact.turn_reasoning).
    dynamic_agent = apply_turn_reasoning(
        state.user_msg, dynamic_agent, app=state.app, session=state.sess
    )
    state.prompt_resolution = dict(dynamic_agent.metadata.get("prompt_resolution") or {})
    state.dynamic_agent_used = dynamic_agent
    execution_mode = f"flow_{(resolution.agent.get('flow') or {}).get('id') or 'react'}"
    state.agent_runtime = _dynamic_agent_runtime_provenance(
        state.app,
        dynamic_agent,
        execution_mode=execution_mode,
    )
    record_turn_reasoning(state.app, state.sid, state.turn_id, state.agent_runtime)
    await emit_semantic_event_async(
        state.app,
        state.sid,
        "agent.toolset.prepare.started",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="running",
        summary=f"Preparing tools for {dynamic_agent.id}.",
        actor={"agent_id": dynamic_agent.id},
        subject={"message_id": state.user_msg.id},
        payload={"agent_id": dynamic_agent.id, "tools": list(dynamic_agent.tools)},
    )
    # The keystone (set_turn_identity) already binds active_app() for the
    # whole turn, so no _gact_app_context wrapper is needed here.
    session_token = _ctx.set_session_id(state.sid)
    try:

        def build_module() -> Any:
            return build_agent_module(state.app.state.agent, dynamic_agent, build_resolution)

        module = await _run_turn_setup_off_loop(state, build_module)
        built = list(getattr(module, "tools", None) or ())
        unlisted = unlisted_tool_names(dynamic_agent.metadata, (t.name for t in built))
        if unlisted:
            # The note introduces the added tools the request's tool list leaves out.
            state.injections = with_tool_introductions(
                state.injections, [t for t in built if t.name in unlisted]
            )
    except Exception:
        await emit_semantic_event_async(
            state.app,
            state.sid,
            "agent.toolset.prepare.failed",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="failed",
            summary=f"Tool preparation failed for {dynamic_agent.id}.",
            actor={"agent_id": dynamic_agent.id},
            subject={"message_id": state.user_msg.id},
            payload={"agent_id": dynamic_agent.id, "tools": list(dynamic_agent.tools)},
        )
        raise
    finally:
        _ctx.reset(session_token)
    await emit_semantic_event_async(
        state.app,
        state.sid,
        "agent.toolset.prepare.completed",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="completed",
        summary=f"Tools ready for {dynamic_agent.id}.",
        actor={"agent_id": dynamic_agent.id},
        subject={"message_id": state.user_msg.id},
        payload={"agent_id": dynamic_agent.id, "tools": list(dynamic_agent.tools)},
    )
    # #1215 S5: agent resolution + module build are done; the LLM
    # request dispatch below is NOT part of agent.resolve.
    bringup_timing.timer_for_session(state.app, state.sid).end_phase("agent.resolve")
    llm_actor = {
        "agent_id": dynamic_agent.id,
        "agent_title": dynamic_agent.title,
        "source": dynamic_agent.source,
        "execution_mode": execution_mode,
    }
    llm_subject = {
        "prompt_id": dynamic_agent.prompt_id,
        "prompt_profile": dynamic_agent.prompt_profile,
        "message_id": state.user_msg.id,
    }
    await emit_semantic_event_async(
        state.app,
        state.sid,
        "llm.request.started",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        status="running",
        summary=f"LLM request started for {dynamic_agent.id}.",
        actor=llm_actor,
        subject=llm_subject,
        provider=_llm_provider_payload(state.app, dynamic_agent.id),
        payload={
            "input": state.enriched_text,
            "prompt_resolution": state.prompt_resolution,
            "agent_runtime": state.agent_runtime,
            "native_image_count": len(state.native_images),
            "native_file_count": len(state.native_files),
        },
    )
    with _cancellation_checker(cancel_cb), _tool_session_context(state.sid, lease_fleet=False):
        state.pred = await await_turn_work(state, _run_module(state, module, cancel_cb))
    await emit_semantic_event_async(
        state.app,
        state.sid,
        "llm.response.completed",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        summary=f"LLM response completed for {dynamic_agent.id}.",
        actor=llm_actor,
        subject=llm_subject,
        provider=_llm_provider_payload(state.app, dynamic_agent.id),
        payload=_prediction_summary(state.pred),
    )
    await emit_semantic_event_async(
        state.app,
        state.sid,
        "agent.invocation.completed",
        turn_id=state.turn_id,
        trace_id=state.trace_id,
        summary=f"{state.invocation_agent_id} returned a prediction.",
        actor={"agent_id": state.invocation_agent_id},
        subject={"message_id": state.user_msg.id},
        payload={
            "selected_expert": getattr(state.pred, "selected_expert", "") or "",
            "route_source": getattr(state.pred, "route_source", "") or "",
            "has_answer": bool(getattr(state.pred, "answer", "") or ""),
            "has_error_info": bool(getattr(state.pred, "error_info", None)),
        },
    )
    return state.pred
