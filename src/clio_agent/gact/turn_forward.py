"""Forward-orchestration seam for the GACT turn engine (#767 Phase B).

Slice 5 of the ``turn.py`` decomposition: the block that resolves the turn's
active agent, builds its DSPy module, runs the streamed-or-synchronous
``forward``, and (for expert packs) settles dynamic-agent delegations moves here
as :func:`forward_turn`, a free function taking
:class:`~clio_agent.gact.turn_state.TurnState` first (the gact seam convention).

The orchestration is byte-for-byte behavior-preserving. :func:`forward_turn`:

* resolves ``state.active_agent_id`` / ``state.invocation_agent_id`` and MUTATES
  them IN PLACE (TRICKY #1): the live chunk emitter was already bound over
  ``state`` (``partial(emit_chunk, state)``) before this seam runs and reads those
  fields *late* whenever a chunk arrives mid-forward, so rebinding a fresh local
  would strand it. The reconstructed ``live_emit`` here is the SAME
  ``partial(emit_chunk, state)`` the highway was bound with, so both the executor
  rail and the streamed forward sites resolve the generating agent identically.
* runs the dynamic-agent (blueprint/prompt/tool) OR the CLIO-orchestrator forward,
  streamed-first with the synchronous executor path as the fallback, setting
  ``state.prompt_resolution`` / ``state.dynamic_agent_used`` / ``state.agent_runtime``
  / ``state.pred`` as the original body did, and returns ``state.pred``. A react
  main routes to its declared children by CALLING the spawn-runtime tools
  (``spawn_agent_task`` / ``wait_agent_tasks``), so ``state.pred`` is already the
  main's own answer — there is no post-forward settle/synthesis pass.

The #714 danger set (the agent-builder + blueprint-runner seams and the executable
session-agent constant) is resolved through ``app`` via a *function-local* import
so the ``app._X`` test monkeypatches keep intercepting with zero test edits; the
cooperative-cancel + no-progress watchdog is reached through
:mod:`clio_agent.gact.turn_watchdog`.
"""

from __future__ import annotations

import asyncio
import contextvars
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.composition import _apply_prompt_registry_to_agent
from clio_agent.gact.agents.resolution import (
    _agent_definition_uses_blueprint_runtime,
    _resolve_runtime_dynamic_agent,
    _runtime_active_agent_blueprint_agent_ids,
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_path,
    _runtime_active_agent_blueprint_root_id,
    _runtime_active_agent_blueprint_rows,
)
from clio_agent.gact.catalog import _builtin_main_agent
from clio_agent.gact.evidence import _dynamic_agent_runtime_provenance
from clio_agent.gact.messaging import _prediction_summary
from clio_agent.gact.providers.auth import _refresh_argonne_lm_token
from clio_agent.gact.runtime import bringup_timing
from clio_agent.gact.runtime.globals import (
    _BlueprintRootDisabled,
    _emit_semantic_event,
    _llm_provider_payload,
    _NoResolvableAgent,
    _session_agent_id,
    _tool_session_context,
    _UnsupportedSessionAgent,
)
from clio_agent.gact.runtime.type_parsing import _blueprint_module_kind
from clio_agent.gact.streaming import (
    _run_dynamic_agent_compat,
    _try_streamed_forward_compat,
)
from clio_agent.gact.turn_stream import emit_chunk
from clio_agent.gact.turn_watchdog import await_turn_work, cancel_requested

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState
    from clio_agent.gact.types import AgentDef
    from clio_agent.prompts import PromptRegistry


def _forward_executor(state: "TurnState") -> Any:
    """The executor a turn's forward should run in: the DEDICATED agent-task pool
    for the CHILD turn's DEPTH (a ``session_type=="agent_task"`` session, #948 S3),
    else ``None`` (the default pool). Routing children onto a PER-DEPTH pool keeps a
    parent blocked in a wait on ``pool[d]`` from starving its own children on
    ``pool[d+1]`` — a single shared pool deadlocks nested orchestrators (#948 S4
    adversarial review)."""

    sess = state.app.state.sessions.get(state.sid)
    meta = (getattr(sess, "metadata", {}) or {}) if sess is not None else {}
    if meta.get("session_type") != "agent_task":
        return None
    block = meta.get("agent_task")
    depth = block.get("depth", 1) if isinstance(block, dict) else 1
    from clio_agent.gact.turn_spawn import agent_task_executor_for_depth  # noqa: PLC0415

    return agent_task_executor_for_depth(state.app, depth)


async def forward_turn(state: "TurnState") -> Any:
    """Run one complete forward/delegation turn under its workspace lease."""

    # #1215 S5: workspace.lease is the OUTER bring-up phase for this whole call
    # (the lease is held for its entire body) -- blueprint.resolve NESTS inside
    # it in _forward_turn_leased below, by necessity: the two are not
    # sequential in real execution (blueprint resolution happens WHILE the
    # lease is held), so treating them as independent depth-0 phases would
    # double-count wall time (see the module docstring's SEAM WIRING note in
    # bringup_timing.py). Nesting is correct here, not a flattening shortcut.
    with bringup_timing.timer_for_session(state.app, state.sid).phase("workspace.lease"):
        # Tool wrappers bind a concrete workspace executor while modules are built.
        # Pin that fleet across initial construction, every delegated child, parent
        # resumes, and the gaps between them; otherwise the idle reaper can close a
        # still-referenced executor during a long multi-expert turn.
        with _tool_session_context(state.sid):
            return await _forward_turn_leased(state)


async def _forward_turn_leased(state: "TurnState") -> Any:
    """Resolve the turn's agent, run its forward, settle delegations; return pred.

    Sets ``state.invocation_agent_id`` / ``state.active_agent_id`` /
    ``state.agent_runtime`` / ``state.prompt_resolution`` /
    ``state.dynamic_agent_used`` / ``state.expert_handoffs`` and returns the
    prediction (``state.pred``). See the module docstring for TRICKY #1/#2.
    """

    # #714 DANGER SET: the agent-builder + blueprint-runner seams are resolved
    # through ``app`` via a *function-local* import so the ~83 ``app._X`` test
    # monkeypatches (which retarget these at call time) keep working with zero
    # test edits. ``_EXECUTABLE_SESSION_AGENT_IDS`` is an ``app``-owned module
    # constant read here too.
    from clio_agent.gact.app import (  # noqa: PLC0415
        _EXECUTABLE_SESSION_AGENT_IDS,
        _blueprint_runner_for_agent,
        _build_blueprint_dspy_module,
        _build_prompt_user_agent_module,
        _build_tool_user_agent_module,
    )

    # TRICKY #1: reconstruct the SAME callable the LM token highway was bound with
    # (``partial(emit_chunk, state)``) — it reads state.active_agent_id /
    # state.invocation_agent_id LATE, so the in-place mutations below are visible.
    live_emit = partial(emit_chunk, state)
    # Cooperative-cancel probe as a zero-arg predicate for the compat shims +
    # _cancellation_checker, exactly like the former closure.
    cancel_cb = partial(cancel_requested, state)

    # #1215 S5: NESTED under workspace.lease (see forward_turn) -- resolves the
    # active agent + builds its DSPy module; end_phase call sits right after the
    # module build below, before the actual LLM request dispatch begins.
    bringup_timing.timer_for_session(state.app, state.sid).start_phase("blueprint.resolve")
    session_agent_id = _session_agent_id(state.sess)
    state.active_agent_id = state.turn_agent_id or session_agent_id
    active_blueprint_root_id = _runtime_active_agent_blueprint_root_id(state.app, state.sid)
    active_blueprint_agent_ids = _runtime_active_agent_blueprint_agent_ids(state.app, state.sid)
    if (
        not state.turn_agent_id
        and active_blueprint_root_id
        and state.active_agent_id in {"", "main", "default"}
    ):
        state.active_agent_id = active_blueprint_root_id
    if (
        active_blueprint_root_id
        and state.active_agent_id == active_blueprint_root_id
        and state.active_agent_id not in active_blueprint_agent_ids
    ):
        # #948 S4: the active blueprint's DECLARED root exists but is disabled
        # (validation errors — e.g. a pre-migration pack whose chain_of_thought
        # main declares children). Fail TYPED here: no silent substitute root,
        # no fall-through to the legacy planner (both observed on the live gate).
        rows = _runtime_active_agent_blueprint_rows(state.app, session_id=state.sid)
        root_errors = next(
            (list(row.validation_errors) for row in rows if row.id == active_blueprint_root_id),
            [],
        )
        blueprint_id = str(rows[0].metadata.get("agent_blueprint_id") or "" if rows else "")
        raise _BlueprintRootDisabled(
            active_blueprint_root_id,
            blueprint_id=blueprint_id,
            validation_errors=root_errors,
        )
    # BARE session (owner ruling 2026-08-05 + RULE 2): the session activated NO
    # blueprint (no id, no path — not merely "nothing resolved"), so it executes
    # the shipped builtin react main from code. An EXPLICITLY activated blueprint
    # that resolves nothing stays on the typed _NoResolvableAgent path below —
    # the builtin main never silently substitutes for a broken activation.
    run_builtin_main = (
        not active_blueprint_root_id
        and state.active_agent_id in _EXECUTABLE_SESSION_AGENT_IDS
        and not _runtime_active_agent_blueprint_id(state.app, state.sid)
        and _runtime_active_agent_blueprint_path(state.app, state.sid) is None
    )
    if run_builtin_main:
        state.active_agent_id = "main"
    routing_mode = getattr(state.sess, "routing_mode", "auto") or "auto"
    state.invocation_agent_id = state.active_agent_id or "orchestrator"
    _emit_semantic_event(
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
            "active_blueprint_root_id": active_blueprint_root_id,
            "active_blueprint_agent_ids": active_blueprint_agent_ids,
        },
    )
    from clio_agent.agent import cancellation_checker as _cancellation_checker  # noqa: PLC0415

    _refresh_argonne_lm_token(state.app.state.agent)

    if (
        run_builtin_main
        or state.active_agent_id not in _EXECUTABLE_SESSION_AGENT_IDS
        or state.active_agent_id in active_blueprint_agent_ids
    ):
        prompt_registry_factory = getattr(state.app.state, "prompt_registry_for_request", None)
        prompt_registry = cast(
            "PromptRegistry | None",
            (
                prompt_registry_factory(session_id=state.sid)
                if callable(prompt_registry_factory)
                else None
            ),
        )
        if run_builtin_main:
            # The builtin main is code, not a discoverable definition: it is
            # materialized here (prompt registry applied like every resolved
            # agent), never via blueprint/pack discovery. A host that cannot
            # execute it fails TYPED at module build (_UnsupportedSessionAgent
            # from _dynamic_agent_tools) — never an empty assistant message.
            dynamic_agent: "AgentDef | None" = _apply_prompt_registry_to_agent(
                state.app, _builtin_main_agent(), prompt_registry=prompt_registry
            )
        else:
            dynamic_agent = _resolve_runtime_dynamic_agent(
                state.app,
                state.active_agent_id,
                session_id=state.sid,
                prompt_registry=prompt_registry,
            )
        if dynamic_agent is None:
            raise _UnsupportedSessionAgent(state.active_agent_id)
        state.prompt_resolution = dict(dynamic_agent.metadata.get("prompt_resolution") or {})
        state.dynamic_agent_used = dynamic_agent
        runner = _blueprint_runner_for_agent(dynamic_agent)
        dynamic_kind = (
            _blueprint_module_kind(dynamic_agent)
            if _agent_definition_uses_blueprint_runtime(dynamic_agent)
            else ""
        )
        execution_mode = (
            f"blueprint_{dynamic_kind}"
            if dynamic_kind
            else ("tool_agent" if dynamic_agent.tools else "prompt_agent")
        )
        state.agent_runtime = _dynamic_agent_runtime_provenance(
            state.app,
            dynamic_agent,
            execution_mode=execution_mode,
        )
        # The keystone (set_turn_identity) already binds active_app() for the
        # whole turn, so no _gact_app_context wrapper is needed here.
        session_token = _ctx.set_session_id(state.sid)
        try:
            module = (
                _build_blueprint_dspy_module(state.app.state.agent, dynamic_agent)
                if _agent_definition_uses_blueprint_runtime(dynamic_agent)
                else (
                    _build_tool_user_agent_module(state.app.state.agent, dynamic_agent)
                    if dynamic_agent.tools
                    else _build_prompt_user_agent_module(state.app.state.agent, dynamic_agent)
                )
            )
        finally:
            _ctx.reset(session_token)
        # #1215 S5: blueprint resolution + module build are done; the LLM
        # request dispatch below is NOT part of blueprint.resolve.
        bringup_timing.timer_for_session(state.app, state.sid).end_phase("blueprint.resolve")
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
        _emit_semantic_event(
            state.app,
            state.sid,
            "llm.request.started",
            turn_id=state.turn_id,
            trace_id=state.trace_id,
            status="running",
            summary=f"LLM request started for {dynamic_agent.id}.",
            actor=llm_actor,
            subject=llm_subject,
            blueprint=dict(state.agent_runtime.get("agent_blueprint") or {}),
            provider=_llm_provider_payload(state.app, dynamic_agent.id),
            payload={
                "request_mode": "streamed",
                "input": state.enriched_text,
                "prompt_resolution": state.prompt_resolution,
                "agent_runtime": state.agent_runtime,
                "native_image_count": len(state.native_images),
            },
        )
        with _cancellation_checker(cancel_cb), _tool_session_context(state.sid):
            state.pred = await await_turn_work(
                state,
                _try_streamed_forward_compat(
                    state.app,
                    state.enriched_text,
                    state.sid,
                    live_emit,
                    session_mode=getattr(state.sess, "mode", "edit"),
                    session_edit_mode=getattr(state.sess, "edit_mode", "diff"),
                    agent_override=module,
                    images=state.native_images,
                    cancel_requested=cancel_cb,
                ),
            )
        if state.pred is not None:
            _emit_semantic_event(
                state.app,
                state.sid,
                "llm.response.completed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                summary=f"LLM response completed for {dynamic_agent.id}.",
                actor=llm_actor,
                subject=llm_subject,
                blueprint=dict(state.agent_runtime.get("agent_blueprint") or {}),
                provider=_llm_provider_payload(state.app, dynamic_agent.id),
                payload=_prediction_summary(state.pred),
            )
        if state.pred is None:
            _emit_semantic_event(
                state.app,
                state.sid,
                "llm.request.started",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                status="running",
                summary=f"Synchronous LLM request started for {dynamic_agent.id}.",
                actor=llm_actor,
                subject=llm_subject,
                blueprint=dict(state.agent_runtime.get("agent_blueprint") or {}),
                provider=_llm_provider_payload(state.app, dynamic_agent.id),
                payload={
                    "request_mode": "sync",
                    "input": state.enriched_text,
                    "prompt_resolution": state.prompt_resolution,
                    "agent_runtime": state.agent_runtime,
                    "native_image_count": len(state.native_images),
                },
            )
            with _cancellation_checker(cancel_cb), _tool_session_context(state.sid):
                loop = asyncio.get_running_loop()
                turn_context = contextvars.copy_context()
                state.pred = await await_turn_work(
                    state,
                    loop.run_in_executor(
                        _forward_executor(state),
                        lambda: turn_context.run(
                            _run_dynamic_agent_compat,
                            runner,
                            state.app.state.agent,
                            dynamic_agent,
                            state.enriched_text,
                            state.sid,
                            cancel_cb,
                        ),
                    ),
                )
            _emit_semantic_event(
                state.app,
                state.sid,
                "llm.response.completed",
                turn_id=state.turn_id,
                trace_id=state.trace_id,
                summary=f"Synchronous LLM response completed for {dynamic_agent.id}.",
                actor=llm_actor,
                subject=llm_subject,
                blueprint=dict(state.agent_runtime.get("agent_blueprint") or {}),
                provider=_llm_provider_payload(state.app, dynamic_agent.id),
                payload=_prediction_summary(state.pred),
            )
    else:
        # #948 S4b: the agent id is in {"", "main", "default"} but the session
        # EXPLICITLY activated a blueprint (id/path set) that resolves no such
        # executable agent — e.g. an activation naming a blueprint that is
        # missing from disk or fully disabled. A bare session never reaches
        # here (it runs the builtin react main above); an explicit activation
        # must not be silently downgraded to the builtin main. The legacy
        # Tier-1 planner (ClioAgent.forward) that used to run here is DELETED,
        # so there is nothing to execute. Fail TYPED — never fall through to a
        # legacy pathway.
        raise _NoResolvableAgent(state.active_agent_id or session_agent_id)
    _emit_semantic_event(
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
