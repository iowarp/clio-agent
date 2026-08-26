"""GACT v0.2 FastAPI application for CLIO.

Exposes the GACT v0.2 contract surface. Most routes are 501 stubs
today; they get wired one at a time in
follow-on iterations against the spec at
``gact-tui/contract/SPEC.md`` and the docs in ``docs/tui/``.

Run via::

    clio-agent-gact --host 127.0.0.1 --port 8100

Or::

    uvicorn clio_agent.gact.app:app --host 127.0.0.1 --port 8100

This is CLIO's single HTTP front door. The legacy ``clio_agent.ui.api``
REST server has been removed; the ``clio-agent-api`` console script is now
a deprecation shim that points here. The CLI (``clio-agent``) is a client
of this same GACT surface.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

# Process diagnostics (SIGUSR1 wedge/heap dump) extracted to gact/diagnostics.py
# (#714 decomposition). Imported + re-exported here; ``_install_sigusr1_diagnostic``
# is invoked at app import below so the handler is wired exactly as before, while
# the single source of truth (and the side-effect-free module) lives in
# diagnostics.py. ``_memprof_dump`` / ``_MEMPROF_STATE`` are re-exported so existing
# ``from clio_agent.gact.app import <name>`` callers keep resolving.
from clio_agent.gact.diagnostics import (  # noqa: E402,F401
    _MEMPROF_STATE,
    _install_sigusr1_diagnostic,
    _memprof_dump,
)

_install_sigusr1_diagnostic()

from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from clio_agent import conf
from clio_agent.gact import context as _ctx
from clio_agent.gact.auth import configure_bearer_auth
from clio_agent.gact.cors import gact_cors_origins as _gact_cors_origins
from clio_agent.gact.error_middleware import install_error_envelope
from clio_agent.gact.protocol.negotiation import install_protocol_negotiation
from clio_agent.gact.runtime.rework_state import initialize_a2ui_store, initialize_session_defaults
from clio_agent.gact.semantic_events import (
    SemanticEventSink,
    build_trace_backend,
)
from clio_agent.prompts import PromptRegistry, PromptSource
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Runtime base re-export shim (#714 decomposition, step 1)                       #
#                                                                               #
# The shared runtime foundation (the ARC singleton + accessors, the semantic-   #
# event funnel, the internal exceptions, the id/timestamp + SSE helpers, and    #
# the ``_ctx`` boundary shims/caches) was carved out into                       #
# ``clio_agent.gact.runtime.globals`` -- the single source every other          #
# extracted module imports FROM (so nothing imports this 24k-line module; the   #
# graph stays acyclic). They are re-exported here so                            #
# ``from clio_agent.gact.app import <name>`` (and ``test_import_seams``) keep    #
# working unchanged. ``runtime.globals`` is the OWNER of ``_PROCESS_ARC`` -- it  #
# is re-exported as a name here, but all LIVE reads/writes happen inside         #
# ``runtime.globals`` (test patch/reset sites target it there).                 #
# --------------------------------------------------------------------------- #
from clio_agent.gact.runtime.globals import (  # noqa: E402, F401
    _ACTIVE_BLUEPRINT_TOOL_ROWS,
    _ACTIVE_GACT_APP,
    _ACTIVE_GACT_SESSION_ID,
    _ACTIVE_GACT_TRACE_ID,
    _ACTIVE_GACT_TURN_ID,
    _PROCESS_ARC,
    ARC_OP_EVENT_TYPE,
    _active_lm_last_reasoning,
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _BlueprintTerminalWorkflowState,
    _build_semantic_event,
    _cancelled_error_info,
    _coerce_error_info,
    _CompatVar,
    _ContextFileAccessError,
    _emit_arc_op,
    _emit_expert_lifecycle_event,
    _emit_react_step_event,
    _emit_semantic_event,
    _format_sse,
    _gact_app_context,
    _iso_from_epoch,
    _llm_provider_payload,
    _new_attempt_id,
    _new_cancellation_attempt_id,
    _new_context_frame_id,
    _new_memory_event_id,
    _new_message_id,
    _new_part_id,
    _new_question_id,
    _not_implemented,
    _process_arc,
    _resolve_tool_session,
    _semantic_trace_id,
    _session_agent_id,
    _set_app_arc,
    _tool_session_context,
    _TurnCancelled,
    _TurnTimedOut,
    _UnsupportedSessionAgent,
    _wire_arc_op_logger,
)

_EXECUTABLE_SESSION_AGENT_IDS = {
    "",
    "main",
    "default",
}


def _web_dir() -> str:
    """Directory of the built web-UI bundle (``paths.web_dir`` / ``CLIO_WEB_DIR``).

    Empty string (the default) means web mode is disabled and the server stays
    headless/TUI-only. Resolved file → env → default like every other knob.
    """

    return conf.resolve("paths.web_dir", env="CLIO_WEB_DIR", default="", cast=conf.as_str).strip()


def _agent_not_available_error(app: "FastAPI", sid: str) -> "ErrorEnvelope":
    """Return a typed error when no executable CLIO agent is ready for a turn."""

    task = getattr(app.state, "agent_construction_task", None)
    task_done = bool(getattr(task, "done", lambda: True)())
    init_error = str(getattr(app.state, "agent_init_error", "") or "")
    want_agent = bool(getattr(app.state, "want_agent", False))

    if want_agent and not task_done:
        status = "starting"
        message = "CLIO is still starting its agent; no agent is ready to accept messages yet."
        recoverable = True
        recovery_actions = ["wait_for_agent_startup", "retry", "check_health"]
    elif init_error:
        status = "failed"
        message = "CLIO agent startup failed; no agent is available to accept messages."
        recoverable = True
        recovery_actions = ["check_server_logs", "fix_lm_configuration", "restart_agent"]
    else:
        status = "not_configured"
        message = (
            "No executable CLIO agent is configured for this backend. Launch `clio-agent-gact` "
            "with an LM provider configured before sending messages."
        )
        recoverable = False
        recovery_actions = ["configure_lm_provider", "restart_agent"]

    details: dict[str, Any] = {
        "session_id": sid,
        "agent_status": status,
        "want_agent": want_agent,
        "recovery_actions": recovery_actions,
    }
    if init_error:
        details["agent_init_error"] = init_error

    return ErrorEnvelope(
        error=ErrorInfo(
            error="agent_not_available",
            message=message,
            details=details,
            recoverable=recoverable,
        )
    )


# Session message-ledger + context-file helpers now live in
# clio_agent.gact.session_store (#714 decomposition). Re-exported here so
# `from clio_agent.gact.app import ...` and test_import_seams stay green.
# Per-turn context enrichment + context-frame provenance now live in
# clio_agent.gact.enrichment (#714 decomposition): context-file injection +
# its structured access error and binary-inspector hook, explicit memory-search
# enrichment, context-frame record/finalize + their token-accounting leaves,
# the approved diffs/apply disk write, and the turn provenance projection.
# Re-exported here so existing ``from clio_agent.gact.app import <name>`` callers
# (the turn engine, tests) + test_import_seams stay green; GactDeps passes
# _apply_edit_to_disk through from this module.
from clio_agent.gact.enrichment import (  # noqa: E402,F401
    _BINARY_CONTEXT_INSPECTORS,
    _apply_edit_to_disk,
    _context_file_access_error,
    _context_file_turn_provenance,
    _enrich_with_context_files,
    _enrich_with_requested_memory_search,
    _estimate_context_tokens,
    _finalize_context_frame,
    _memory_search_request_from_message,
    _message_text_for_frame,
    _record_context_frame,
)
from clio_agent.gact.metrics_counters import MetricsCounters  # noqa: E402
from clio_agent.gact.runtime.retention import init_retention_state  # noqa: E402
from clio_agent.gact.session_store import (  # noqa: E402,F401
    _append_session_message,
    _compile_session_conversation_history,
    _delete_session_context_files,
    _delete_session_messages,
    _extend_session_messages,
    _flush_context_files,
    _load_context_files,
    _release_session_arc,
    _replace_session_messages,
)


def _cancellation_attempt_summary(attempt: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attempt:
        return {}
    return {
        key: attempt[key]
        for key in (
            "id",
            "session_id",
            "requested_at",
            "in_flight",
            "cooperative_signal_sent",
            "asyncio_task_cancel_scheduled",
            "asyncio_task_cancel_sent",
            "hard_abort_supported",
            "upstream_abort",
            "executor_work_may_continue",
        )
        if key in attempt
    }


def _enrich_cancellation_error_info(
    app: "FastAPI",
    sid: str,
    error_info: "ErrorInfo | None",
) -> "ErrorInfo | None":
    """Attach durable cancellation-attempt evidence to cancelled turns."""

    if error_info is None or error_info.error != "cancelled":
        return error_info
    attempts = getattr(app.state, "cancel_attempts", None)
    attempt = attempts.get(sid) if isinstance(attempts, Mapping) else None
    if not attempt:
        return error_info
    details = error_info.details
    details.setdefault("cancellation_attempt_id", attempt.get("id", ""))
    details.setdefault("cancellation_attempt", _cancellation_attempt_summary(attempt))
    details.setdefault("hard_abort_supported", attempt.get("hard_abort_supported", False))
    details.setdefault("upstream_abort", attempt.get("upstream_abort", "not_supported"))
    return error_info


# --------------------------------------------------------------------------- #
# Agent resolution + prompt composition re-export shims (#714 step 5/A).         #
#                                                                               #
# The stateless agent/blueprint/expert-pack RESOLUTION queries and the prompt   #
# COMPOSITION / dynamic-context renderers were carved out into                   #
# ``clio_agent.gact.agents.resolution`` and ``clio_agent.gact.agents.composition``#
# (each takes ``app`` explicitly; both import only the shared runtime base +     #
# gact leaves, never this module). They are re-exported here so                  #
# ``from clio_agent.gact.app import <name>`` keeps working unchanged. The owner  #
# modules are the single source of truth; tests that patch these must target the #
# owner (``...agents.resolution`` / ``...agents.composition``), not this shim.   #
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Extracted-module re-export shims (#714 decomposition)                         #
#                                                                               #
# The cohesive helper clusters below were carved OUT of this module into        #
# sibling modules (the single source of truth for each). They are re-imported   #
# here so existing ``from clio_agent.gact.app import <name>`` callers (tests,   #
# gact/turn.py, routes/deps.py, agents/builders.py) + the ``test_import_seams`` #
# guardrail keep resolving them through this stable shim. None of these sibling #
# modules import this 24k-line module, so the graph stays acyclic. The imports  #
# are isort-sorted; each per-module comment annotates the run it heads.         #
# (behavior-preserving extraction)                                              #
# --------------------------------------------------------------------------- #
# gact/_params.py -- user-agent generation-parameter parsing.
from clio_agent.gact import provenance_wiring, relay_wiring  # noqa: E402
from clio_agent.gact._params import (  # noqa: E402,F401
    _gact_turn_timeout_s,
    _semantic_trace_detail_level,
    _user_agent_bool_param,
    _user_agent_float_param,
    _user_agent_int_param,
    _user_agent_param,
)
from clio_agent.gact.agents import resolution as _resolution  # noqa: E402, F401

# gact/agents/builders.py + agents/runtime.py -- expert/blueprint runtime engine;
# the kept turn-handler dispatch wrappers below reach the builders through these.
from clio_agent.gact.agents.builders import (  # noqa: E402,F401
    _active_base_agent_tool_executor,
    _adapter_tool_intent_from_exception,
    _blueprint_runtime_signature,
    _build_blueprint_dspy_module,
    _build_prompt_user_agent_module,
    _build_tool_user_agent_module,
    _call_enabled_external_mcp_tool,
    _call_recovered_dspy_tool,
    _dynamic_agent_lm_config,
    _dynamic_agent_tools,
    _emit_blueprint_llm_failure,
    _emit_invalid_tool_selection_event,
    _enabled_external_mcp_dspy_tools,
    _extract_repair_attempts,
    _invalid_tool_selection_from_exception,
    _is_repairable_typed_output_error,
    _prompt_user_agent_signature,
    _recording_blueprint_tool,
    _recover_blueprint_react_tool_intent,
    _repair_temperature,
    _run_external_mcp_tool_sync,
    _tool_names,
    _tool_user_agent_max_iters,
    _tool_user_agent_signature,
    _typed_output_repair_hint,
)
from clio_agent.gact.agents.composition import (  # noqa: E402, F401
    _agent_prompt_request,
    _agent_rows_prompt_render_context,
    _apply_prompt_registry_to_agent,
    _prompt_render_context,
    _prompt_resolution_metadata,
    _runtime_active_workspace_context,
    _runtime_dynamic_agent_children_context,
)
from clio_agent.gact.agents.invoker import InProcessExpertInvoker  # noqa: E402
from clio_agent.gact.agents.resolution import (  # noqa: E402, F401
    _agent_definition_is_agent_blueprint,
    _agent_definition_uses_blueprint_runtime,
    _agent_overlay_patchable_fields,
    _agent_with_capability_refs,
    _merge_agent_def_rows,
    _resolve_dynamic_agent,
    _resolve_runtime_dynamic_agent,
    _runtime_active_agent_blueprint_agent_ids,
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_path,
    _runtime_active_agent_blueprint_root_id,
    _runtime_active_agent_blueprint_rows,
    _runtime_active_session_expert_pack_id,
    _runtime_active_session_expert_pack_path,
    _runtime_apply_session_agent_overlay,
    _runtime_child_agent_rows,
    _runtime_declared_child_ids,
    _runtime_session_agent_overlay,
    _runtime_workspace_catalog_cwd,
)
from clio_agent.gact.agents.runtime import (  # noqa: E402,F401
    _prediction_structured_metadata,
    _retaining_react_cls,
    _summarize_segments_llm,
)

# gact/delegation.py -- delegation + workflow-state derivation cluster.
from clio_agent.gact.delegation import (  # noqa: E402,F401
    _coerce_expert_handoff_rows,
    _compact_exact_evidence_index,
    _expert_handoff_fields,
    _json_objects_from_text,
    _merge_workflow_state_from_value,
    _prediction_workflow_state,
    _workflow_state_from_handoff_rows,
    _workflow_state_from_outputs,
    _workflow_state_payload,
)

# gact/evidence.py -- evidence-grounding + tool-result / trajectory-evidence.
from clio_agent.gact.evidence import (  # noqa: E402,F401
    _bounded_tool_call_result,
    _dynamic_agent_runtime_provenance,
    _extract_tools_called_from_trajectory,
    _is_bounded_tool_result,
    _is_empty_dynamic_agent_answer_error,
    _propose_edit_diffs_from_pred,
    _tool_agent_empty_answer_fallback,
    _tool_result_is_error,
    _tool_result_preview,
)
from clio_agent.gact.mcp_apps import (  # noqa: E402
    cleanup_all_mcp_apps,
    install_mcp_app_runtime,
    register_mcp_app_routes,
)

# gact/messaging.py -- message / multimodal + ask-user + trace-summary helpers.
from clio_agent.gact.messaging import (  # noqa: E402,F401
    _agent_accepts_images,
    _ask_user_options_from_action,
    _ask_user_resume_text,
    _coerce_ask_user_action,
    _dspy_images_from_parts,
    _format_subagent_input,
    _image_part_summaries,
    _prediction_summary,
    _user_message_parts,
)

# Provider / LM-bind helpers moved to gact/providers/ (#714 decomposition step 6).
# Re-exported here so existing ``from clio_agent.gact.app import <name>`` callers +
# the import-seam guardrail (``_refresh_argonne_lm_token`` pinned) stay green; the
# write-side ``PUT /v1/providers/lm`` bind closures still live in the provider route
# handler below and move with the route extraction (step 7).
from clio_agent.gact.providers.auth import (  # noqa: E402,F401
    _is_placeholder_api_key,
    _refresh_argonne_lm_token,
    _resolve_argonne_runtime_api_key,
)
from clio_agent.gact.providers.config import (  # noqa: E402,F401
    _active_lm_model_ref,
    _active_lm_supports_vision,
    _current_lm_model_id,
    _effective_lm_config,
    _image_part_error,
    _model_ref_dict,
    _model_ref_is_empty,
    _model_ref_matches_active,
    _provider_runtime_kind,
    _unsupported_model_ref_error,
)
from clio_agent.gact.providers.lmstudio import (  # noqa: E402,F401
    _lm_studio_api_root,
    _lm_studio_headers,
    _release_owned_lm_studio_instance,
)
from clio_agent.gact.routes import artifact_workspace  # noqa: E402
from clio_agent.gact.routes.a2ui import register_a2ui_routes  # noqa: E402
from clio_agent.gact.routes.agent_tasks import (  # noqa: E402
    register_agent_task_routes,
)
from clio_agent.gact.routes.agents import (  # noqa: E402
    register_agents_routes,
)
from clio_agent.gact.routes.async_processes import (  # noqa: E402
    register_async_process_routes,
)
from clio_agent.gact.routes.blueprints import (  # noqa: E402
    register_blueprints_routes,
)
from clio_agent.gact.routes.catalog import (  # noqa: E402
    register_catalog_routes,
)
from clio_agent.gact.routes.context import (  # noqa: E402
    register_context_routes,
)
from clio_agent.gact.routes.deps import GactDeps  # noqa: E402
from clio_agent.gact.routes.diffs import (  # noqa: E402
    register_diffs_routes,
)
from clio_agent.gact.routes.expert_packs import (  # noqa: E402
    register_expert_packs_routes,
)
from clio_agent.gact.routes.mcp import (  # noqa: E402
    register_mcp_routes,
)
from clio_agent.gact.routes.memory import (  # noqa: E402
    register_memory_routes,
)
from clio_agent.gact.routes.messages import (  # noqa: E402
    register_messages_routes,
)
from clio_agent.gact.routes.misc import (  # noqa: E402
    register_misc_routes,
)
from clio_agent.gact.routes.permissions import (  # noqa: E402
    register_permissions_routes,
)
from clio_agent.gact.routes.prompts import (  # noqa: E402
    register_prompts_routes,
)
from clio_agent.gact.routes.provenance import register_provenance_routes  # noqa: E402
from clio_agent.gact.routes.provider_models_refresh import (
    register_provider_models_refresh_routes,  # noqa: E402
)
from clio_agent.gact.routes.providers import register_providers_routes  # noqa: E402
from clio_agent.gact.routes.relay import register_relay_routes  # noqa: E402
from clio_agent.gact.routes.schedules import (  # noqa: E402
    register_schedules_routes,
)
from clio_agent.gact.routes.session_defaults import (  # noqa: E402
    register_session_defaults_routes,
)
from clio_agent.gact.routes.sessions import register_sessions_routes  # noqa: E402
from clio_agent.gact.routes.system import register_system_routes  # noqa: E402
from clio_agent.gact.routes.trace import register_trace_routes  # noqa: E402
from clio_agent.gact.routes.workspaces import (  # noqa: E402
    register_workspaces_routes,
)

# Capability + metrics catalogs (the stream-fallback reason catalog, the
# capability-gap rows, and the latency-stat percentile helper) moved to
# gact/runtime/capabilities.py (#714 decomposition) so the read-only system
# routes (routes/system.py) and the message-turn streaming path here share one
# source. The turn path reads ``_STREAM_FALLBACK_REASON_DEFINITIONS`` via
# ``_stream_fallback_payload`` below; re-exported so existing
# ``from clio_agent.gact.app import <name>`` callers stay green.
from clio_agent.gact.runtime.capabilities import (  # noqa: E402,F401
    _CAPABILITY_GAP_DEFINITIONS,
    _STREAM_FALLBACK_REASON_DEFINITIONS,
    _capability_gap_metadata,
    _latency_stat,
    _stream_fallback_reason_capabilities,
)

# Slash-command table assembly moved to gact/runtime/commands.py (#714
# decomposition) so routes/catalog.py and the prompt-render-context closure here
# share one source. Imported under the legacy underscore names the render-context
# closure already used.
from clio_agent.gact.runtime.commands import (  # noqa: E402
    command_cwd_for_request as _command_cwd_for_request,
)
from clio_agent.gact.runtime.commands import (  # noqa: E402
    planner_command_rows as _planner_command_rows,
)

# Server-wide wire + limit constants (contract/backend version, inline-context
# byte cap) moved to gact/runtime/constants.py (#714 decomposition) so the route
# modules read them without importing back into app.py. Re-exported so existing
# ``from clio_agent.gact.app import <name>`` callers stay green.
from clio_agent.gact.runtime.constants import (  # noqa: E402,F401
    _CTX_MAX_BYTES,
    CONTRACT_VERSION,
    GACT_BACKEND_VERSION,
)

# Token / context-window leaf machinery moved to gact/runtime/context_tokens.py
# (#714 decomposition step 2). Re-exported here so existing
# ``from clio_agent.gact.app import <name>`` callers + the import-seam guardrail
# stay green; the expert forward (step 4) imports these from the new module.
from clio_agent.gact.runtime.context_tokens import (  # noqa: E402,F401
    _CONTEXT_CATEGORY,
    _arc_obs_value,
    _autocompact_threshold,
    _bucket_context_categories,
    _estimate_text_tokens,
    _last_prompt_tokens,
    _resolve_expert_context_window,
)

# Transcript-memory search primitives (query normalization, excerpting, the
# scope-controlled ranked search) + the shared message-excerpt projection moved
# to gact/runtime/memory_search.py (#714 decomposition) so the agent-run path
# (_enrich_with_requested_memory_search / _compile_session_conversation_history)
# and the memory routes (routes/memory.py) share one implementation. Re-exported
# here so existing ``from clio_agent.gact.app import <name>`` callers stay green.
from clio_agent.gact.runtime.memory_search import (  # noqa: E402,F401
    _memory_search_excerpt,
    _memory_search_response,
    _memory_search_terms,
    _message_text_excerpt,
)

# Permission-policy data machinery (validation, load/flush, resolution-derived
# policy + the constants) moved to gact/runtime/permission_policies.py (#714
# decomposition step 7) so the permissions route module + this startup path share
# one implementation. Re-exported here so existing
# ``from clio_agent.gact.app import <name>`` callers stay green; the in-app gate
# enforcement (_policy_action_for_tool / _guard_direct_destructive_action) and
# build_app startup import these from the new module.
from clio_agent.gact.runtime.permission_policies import (  # noqa: E402,F401
    _PERMISSION_POLICY_ACTIONS,
    _PERMISSION_POLICY_SCOPES,
    _append_permission_policy_from_resolution,
    _flush_permission_policies,
    _load_permission_policies,
    _permission_path_from_args,
    _validate_permission_policies,
)
from clio_agent.gact.runtime.type_parsing import (  # noqa: E402,F401
    _SCALAR_FIELD_TYPES,
    _blueprint_module_kind,
    _is_optional_annotation,
    _parse_field_annotation,
    _sanitize_model_name,
)

# gact/workflow_state/merge.py -- pure workflow_state merge/normalize helpers.
from clio_agent.gact.workflow_state.merge import (  # noqa: E402,F401
    _TRAJECTORY_TOOL_ARGS_KEYS,
    _TRAJECTORY_TOOL_NAME_KEYS,
    _TRAJECTORY_TOOL_RESULT_KEYS,
    _UNICODE_PATH_HYPHENS,
    _merge_inferred_workflow_state,
    _merge_non_empty_mapping,
    _merge_workflow_state_mapping,
    _normalize_pathlike_text,
    _normalize_workflow_state_scalar,
    _trajectory_key_index,
    _value_has_semantic_content,
)


def _run_blueprint_dspy_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
) -> Any:
    token = _ctx.set_session_id(session_id)
    try:
        module = _build_blueprint_dspy_module(base_agent, agent_def)
        return module(
            question=question,
            session_id=session_id,
            cancel_requested=cancel_requested,
        )
    finally:
        _ctx.reset(token)


def _blueprint_runner_for_agent(agent_def: "AgentDef") -> Any:
    if _agent_definition_uses_blueprint_runtime(agent_def):
        return _run_blueprint_dspy_agent
    return _run_tool_user_agent if agent_def.tools else _run_prompt_user_agent


def _run_prompt_user_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
) -> Any:
    """Execute a prompt-only user/skill agent through DSPy/LiteLLM."""
    token = _ctx.set_session_id(session_id)
    try:
        module = _build_prompt_user_agent_module(base_agent, agent_def)
        return module.forward(
            question=question,
            session_id=session_id,
            cancel_requested=cancel_requested,
        )
    finally:
        _ctx.reset(token)


def _run_tool_user_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
) -> Any:
    """Execute a tool-declaring user/skill agent through DSPy ReAct."""
    token = _ctx.set_session_id(session_id)
    try:
        module = _build_tool_user_agent_module(base_agent, agent_def)
        return module.forward(
            question=question,
            session_id=session_id,
            cancel_requested=cancel_requested,
        )
    finally:
        _ctx.reset(token)


def _clear_session_model_refs(app: "FastAPI") -> None:
    """Clear stale default/session model refs after a global LM provider swap."""

    if (defaults := getattr(app.state, "session_defaults", None)) is not None:
        defaults.clear_model_ref()
    if (sessions := getattr(app.state, "sessions", None)) is not None:
        for sess in sessions.list():
            if not _model_ref_is_empty(sess.model):
                sessions.update(sess.id, model={})


# --------------------------------------------------------------------------- #
# Turn-orchestration engine extracted to gact/turn.py (#714 decomposition).      #
#                                                                               #
# ``_run_turn_in_background`` (the off-thread turn loop) and                     #
# ``_start_background_user_turn`` (the staging                                   #
# entrypoint) were carved out verbatim into ``clio_agent.gact.turn`` so the      #
# route factories + the scheduler tick can share the entrypoint without          #
# importing back into this module. They are re-exported here so existing         #
# ``from clio_agent.gact.app import <name>`` callers + the import-seam guardrail  #
# stay green; ``_start_background_user_turn`` is the explicit-``app`` engine the  #
# thin ``build_app`` closure wrapper (and ``GactDeps``) delegate to.             #
# --------------------------------------------------------------------------- #
from clio_agent.gact.agent_tasks import (  # noqa: E402
    install_agent_task_registry,
)
from clio_agent.gact.mcp_task_events import install_mcp_task_event_publisher  # noqa: E402
from clio_agent.gact.turn import (  # noqa: E402,F401
    _run_turn_in_background,
    _start_background_user_turn,
)
from clio_agent.gact.turn_runner import (  # noqa: E402
    drain_app_turns,
    install_turn_runner,
)
from clio_agent.gact.turn_spawn import (  # noqa: E402
    install_agent_task_executor,
    shutdown_agent_task_executors,
)

# Alias kept so the thin ``build_app`` closure wrapper (which shadows the
# ``_start_background_user_turn`` name locally) can still reach the explicit-``app``
# engine; the unaliased name above is the module-level re-export the import-seam
# guardrail + ``from clio_agent.gact.app import _start_background_user_turn`` use.
_turn_start_background_user_turn = _start_background_user_turn


# gact/usage.py -- usage/cost metering: per-LM history-diff + UsageTracker
# rollups, reasoning-record extraction, and the best-effort price table.
# Tool permission gating + cancellation moved to gact/permission_gate.py
# (#714 decomposition): destructive-tool classification, the bounded
# shell-diagnostic fast-allow analysis, permission-policy enforcement +
# resolved-permission audit rows, the direct-destructive-action guard, the
# interactive permission gate, and the cancellation checker. Re-exported here
# so existing ``from clio_agent.gact.app import <name>`` callers + test seams
# stay green; build_app's app.state.make_permission_gate seam and GactDeps
# (_guard_direct_destructive_action) import these from the new module.
# --- re-export shim (#714): skills/commands/catalog loading moved to catalog.py ---
from typing import Protocol

from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    load_agent_blueprint_path,
    load_agent_blueprints,
    read_install_metadata,
)
from clio_agent.gact.catalog import (  # noqa: E402, F401
    _builtin_agents,
    _builtin_tools,
    _command_search_roots,
    _load_command_files_from_disk,
    _normalize_file_command_id,
    _parse_skill_frontmatter,
    _tool_owner_for_catalog,
    _tool_tags_for_catalog,
    _tool_visible_to_for_catalog,
    _truthy_command_field,
)
from clio_agent.gact.events import EventBus
from clio_agent.gact.expert_packs import (
    discover_expert_packs,
    load_expert_pack_path,
    load_expert_packs,
    validate_expert_hierarchy,
)
from clio_agent.gact.loop_inbox import _make_loop_inbox_drain, drain_inbox_to_new_turn
from clio_agent.gact.messages import MessageStore
from clio_agent.gact.permission_gate import (  # noqa: E402,F401
    _direct_permission_denied,
    _guard_direct_destructive_action,
    _make_cancellation_checker,
    _make_permission_gate,
    _policy_action_for_tool,
    _record_resolved_permission,
)
from clio_agent.gact.resident_ledgers import build_resident_ledger_set, seed_metrics_counters
from clio_agent.gact.sessions import SessionStore, _default_store_path
from clio_agent.gact.skills import SkillNotDelegatableError

# Live-streaming + prediction-rendering cluster (#714 decomposition) moved to
# gact/streaming.py: signature-compatible agent invocation, the DSPy streamify
# pump + structured fallback ledger, stream-listener binding + streamability
# gating, chunk/text extraction, and prediction rendering (trajectory / tools /
# signature docstring). Re-exported here so existing
# ``from clio_agent.gact.app import <name>`` callers + test seams stay green; in
# particular the turn path + agents/builders import these via this module, and
# ``_try_streamed_forward_compat`` resolves ``_try_streamed_forward`` back
# through this re-export so the ``monkeypatch.setattr(
# "clio_agent.gact.app._try_streamed_forward", ...)`` test seam keeps working.
from clio_agent.gact.streaming import (  # noqa: E402,F401
    _REASONING_HEARTBEAT_S,
    _agent_streaming_unsupported_reason,
    _append_stream_listener,
    _build_stream_listeners,
    _chunk_reasoning_text,
    _chunk_text,
    _config_is_reasoning_model,
    _describe_stream_exc,
    _extract_tools_called,
    _format_react_trajectory,
    _pop_stream_fallback,
    _record_stream_fallback,
    _run_dynamic_agent_compat,
    _signature_prompt,
    _stream_fallback_payload,
    _stream_fallback_reasons,
    _stream_response_prefix,
    _StreamingOutputError,
    _try_streamed_forward,
    _try_streamed_forward_compat,
)

# gact/tool_observer.py -- tool-observer + live-assistant transcript cluster.
# Re-exported here so existing ``from clio_agent.gact.app import <name>`` callers
# + test seams stay green; build_app's app.state.make_tool_observer seam and the
# GactDeps(install_tool_runtime_hooks) seam import these from the new module so
# the seams keep working post-decomposition (#714).
from clio_agent.gact.tool_observer import (  # noqa: E402,F401
    _OBSERVER_CALL_IDS,
    _OBSERVER_CALL_T0,
    _agent_tool_owner,
    _append_live_assistant_part,
    _append_live_assistant_part_once,
    _emit_live_tool_route_context,
    _ensure_live_assistant_message,
    _install_tool_runtime_hooks,
    _make_tool_observer,
    _merge_tool_call_rows,
    _normalize_tool_call_row,
    _tool_call_event_key,
    _tool_call_has_result_evidence,
    _tool_call_name_args_key,
    _tool_calls_from_handoff_rows,
)
from clio_agent.gact.transcript import TurnTranscriptRegistry
from clio_agent.gact.types import (
    AgentDef,
    ErrorEnvelope,
    ErrorInfo,
    Message,
    Part,
    Session,
)
from clio_agent.gact.usage import (  # noqa: E402,F401
    _PRICE_TABLE_PER_M,
    _all_known_lms,
    _entry_prompt_text,
    _entry_reasoning_text,
    _entry_response_text,
    _estimate_cost_usd,
    _reasoning_records_from_history_slice,
    _snapshot_lm_history_index,
    _usage_from_dspy_history,
    _usage_from_history_slice,
)
from clio_agent.gact.workspaces import (
    WorkspaceStore,
)
from clio_agent.gact.workspaces import (
    _default_store_path as _ws_default_store_path,
)


class AgentLike(Protocol):
    """Structural interface for anything the GACT POST-message path
    can drive. Lets tests inject a fake without pulling DSPy + a real
    LM; production wires the actual ``ClioAgent``.

    ``forward`` MUST return something with ``.answer`` (str) and
    ``.selected_expert`` (str). The real ``dspy.Prediction`` already
    matches this shape; FakeClioAgent in the tests does too.
    """

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover
        ...


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook.

    Spins the scheduler tick task (#21) at boot if a ScheduleStore
    is wired; cancels it cleanly on shutdown.

    Also kicks off deferred ClioAgent construction when the runner
    set ``app.state.want_agent`` (see ``main()``). The agent's heavy
    init (DSPy + ARC + experts) used to block uvicorn's startup, which
    pushed first /v1/capabilities response past gact-tui's 3-second
    deploy probe. Now we bind the port immediately, finish boot in a
    background task, and POST /messages keeps 503-ing until
    ``app.state.agent`` is stamped.
    """

    app.state.started_at = time.time()
    app.state.mcp_app_loop = asyncio.get_running_loop()
    # #948 S1 (#662): anchor turn tasks to THIS app-lifetime loop, not whatever
    # transient request/portal loop submits them.
    app.state.turn_runner.bind_loop(app.state.mcp_app_loop)
    # #900: bind CLIO's child tree (MCP stdio + pooled SDK CLI) to this server so a HARD
    # kill reaps it (Windows Job Object / POSIX pdeathsig). Typed result → doctor probe.
    from clio_agent.runtime.process_tree import install_child_reaper  # noqa: PLC0415

    app.state.child_reaper = install_child_reaper()

    # #975: resolve the OS write-confinement backend (floor-first `none` + typed reason;
    # owner module runtime/sandbox.py). The boot `sandbox.state` event fires once ARC is
    # live (in _construct_agent_async).
    from clio_agent.runtime.sandbox import install_sandbox  # noqa: PLC0415

    app.state.sandbox = install_sandbox()

    # #1232 pt 4 + #1001: reap provably-orphaned clio-launched children (dead
    # parent + clio identity; the daemon root is excluded by construction)
    # BEFORE the MCP-cache prune's peer-liveness check runs — a still-running
    # orphan from a prior hard kill otherwise looks like a live peer to
    # ``live_peer_clio_processes`` and defers the prune indefinitely (the
    # observed "deferred for two days" bug). Sequenced (not raced) so the
    # ordering is real, still fully off-loop/best-effort/typed-logged.
    from clio_agent.runtime.process_census import boot_reap_off_loop  # noqa: PLC0415
    from clio_agent.tools.mcp_cache import boot_prune_off_loop  # noqa: PLC0415

    async def _reap_orphans_then_prune_mcp_cache() -> None:
        await boot_reap_off_loop()
        await boot_prune_off_loop()

    app.state.mcp_cache_prune_task = asyncio.create_task(_reap_orphans_then_prune_mcp_cache())

    task: Optional[asyncio.Task] = None
    if getattr(app.state, "schedules", None) is not None:
        task = asyncio.create_task(_scheduler_tick(app))
        app.state.scheduler_task = task

    agent_task: Optional[asyncio.Task] = None
    if getattr(app.state, "want_agent", False) and app.state.agent is None:
        agent_task = asyncio.create_task(_construct_agent_async(app))
        app.state.agent_construction_task = agent_task

    yield

    # #948 S1: the turn-runner idle hook is ALSO a turn producer — a draining
    # turn's completion would otherwise re-drive a deferred resume, staging a fresh
    # turn whose task the drain hard-cancels but whose SIDE EFFECTS (a persisted
    # user message, the session flipped to 'running', a misleading
    # ``user_question.resumed`` event) survive. Unregister it before draining so no
    # completion during teardown stages new work. The deferred resume is simply
    # not run (honest: no false 'resumed' claim), consistent with shutdown losing
    # other in-memory in-flight state.
    app.state.turn_runner.set_idle_hook(None)
    await asyncio.to_thread(app.state.document_store.close)
    # #948 S1 (#662): quiesce the internal turn-PRODUCERS (the scheduler tick, the
    # agent-construction and lm-config tasks) BEFORE draining turns, so nothing can
    # spawn a fresh turn into the drain window. The scheduler is the one live
    # producer post-yield (request callers are gone once uvicorn stops serving);
    # left running it could fire a due schedule mid-drain and leave a zombie turn
    # the drain never saw. (drain() also re-snapshots to catch any stray late spawn.)
    lm_config_task = getattr(app.state, "lm_config_task", None)
    for t in (task, agent_task, lm_config_task):
        if t is None:
            continue
        if getattr(t, "done", lambda: False)():
            continue
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001,S110 - shutdown task drain; cancellation/errors ignored on teardown
            pass

    # Now deterministically settle in-flight turns, while the bus / sessions / ARC
    # they persist into are still alive (owner module does the cooperative-cancel +
    # bounded-grace + typed-reason drain).
    await drain_app_turns(app, logger)

    # #948 S3/S4: shut down every per-depth agent-task pool (child forwards) off the
    # loop, symmetric to their lazy install. Without this their non-daemon workers
    # leak across app lifecycles and a worker still in a slow child forward blocks
    # process exit (concurrent.futures' atexit joins all workers).
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: shutdown_agent_task_executors(app)
        )
    except Exception:  # noqa: BLE001,S110 - defensive shutdown cleanup
        pass

    # MCP Apps may own browser attachments or other remote resources. Close
    # every retained record while the exact originating MCP transports are
    # still alive. A failure is logged and retained; the child server's own
    # close_all lifespan remains the final process-level backstop below.
    try:
        await cleanup_all_mcp_apps(app)
    except RuntimeError:
        logger.exception(
            "one or more MCP Apps failed explicit host cleanup; "
            "child transport shutdown will run the server cleanup backstop"
        )
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _release_owned_lm_studio_instance(app, raise_on_error=False),
        )
    except Exception:  # noqa: BLE001,S110 - best-effort LM-Studio instance release on shutdown
        pass
    # Drain + stop the off-loop semantic-trace writer so no events are lost on shutdown.
    _trace_backend = getattr(app.state, "semantic_trace_backend", None)
    _trace_close = getattr(_trace_backend, "close", None)
    if callable(_trace_close):
        try:
            _trace_close()
        except Exception:  # pragma: no cover - defensive shutdown cleanup  # noqa: BLE001,S110 - defensive shutdown cleanup
            pass
    provenance_wiring.close_artifact_backend(app)
    # #900: explicit clean-shutdown teardown (promoted from atexit best-effort) — close
    # the agent's MCP stdio executors + pooled SDK CLI transports now, with typed logging,
    # off the event loop (thread joins). A HARD kill skips this; the Job Object / pdeathsig
    # binding above is the backstop. The owner helper never raises.
    from clio_agent.runtime.process_tree import shutdown_child_processes  # noqa: PLC0415

    _agent = getattr(app.state, "agent", None)
    await asyncio.get_running_loop().run_in_executor(None, lambda: shutdown_child_processes(_agent))
    # NOTE: the shared clio-core runtime client is released (last-one-out stop) via the
    # atexit hook registered in ClioCoreStore — NOT here. uvicorn handles SIGTERM by exiting
    # the serve loop and returning normally, so the interpreter exits and atexit fires
    # ("I leave the TUI, everything gets released"). Doing it in this lifespan hook would
    # wrongly stop the SHARED daemon on any app teardown that is not a process exit
    # (e.g. a second app in the same process), which the atexit path correctly avoids.


async def _construct_agent_async(app: "FastAPI") -> None:
    """Build the real ClioAgent off the lifespan hot path.

    DSPy import, ARC hydration, and expert wiring take about 10 seconds on
    Aurora, so ``run_in_executor`` keeps the event loop available. Success
    publishes ``app.state.agent`` and ``app.state.arc``; failure leaves the
    agent unset so message requests continue returning a structured 503
    instead of observing a partially built agent.
    """

    loop = asyncio.get_running_loop()
    # Construct (or reuse) the ONE per-process ARC up front and inject it into the build,
    # so the agent does not mint a fresh ARC — the same instance is app.state.arc for the
    # whole process across every later LM bind (no per-build ARC churn / trace ⊋ ARC split).
    arc = _process_arc(app)
    relay_kwargs = await relay_wiring.relay_agent_kwargs(app)

    def _build() -> Any:
        import dspy  # noqa: PLC0415

        from clio_agent.agent import ClioAgent  # noqa: PLC0415
        from clio_agent.config import (  # noqa: PLC0415
            create_chat_adapter,
            create_lm,
            load_config_from_env,
        )

        cfg = load_config_from_env()
        # Boot-time process-global dspy default: a HARMLESS ambient fallback only
        # (design §6). It is never rewritten on a per-expert path; the main agent
        # and every expert select their LM per-call via ``dspy.context``. Kept so
        # any un-wrapped ambient caller still has a valid LM.
        dspy.configure(
            lm=create_lm(cfg),
            adapter=create_chat_adapter(cfg),
        )
        # Drop the boot env-handoff (design §9 step 9): hand the ONE boot config to
        # ClioAgent instead of letting it read the environment a SECOND time. The
        # main agent binds ``_main_lm`` / ``_planner_lm`` / ``_dspy_adapter`` off
        # this exact config (credential included — the boot/default config is the
        # sanctioned env-credential read, design §6), so a GACT booted purely from
        # ``CLIO_LM_*`` still authenticates.
        agent = ClioAgent(verbose=False, arc=arc, provider_config=cfg, **relay_kwargs)
        # Make the ProviderProfileStore the authoritative identity registry:
        # reseed its default from the agent's FINAL resolved config (post
        # lm_studio model discovery) so the store's default profile and
        # ``ClioAgent._main_lm`` are the SAME identity, and every expert inherits
        # exactly what the main agent runs (design §9 step 9). build_app already
        # seeded a default; this keeps the store consistent via an atomic swap.
        from clio_agent.gact.providers.profile_store import ProviderProfileStore
        from clio_agent.providers.lm_spec import spec_from_config

        existing = getattr(app.state, "provider_profiles", None)
        default_spec = spec_from_config(agent._provider_config)
        app.state.provider_profiles = (
            existing.with_default(default_spec)
            if isinstance(existing, ProviderProfileStore)
            else ProviderProfileStore.seed(default_spec)
        )
        return agent

    # Pre-import the heavy LM stack ON THIS THREAD before any builder thread
    # runs: the deferred init here and a concurrent provider bind
    # (construct_agent_with_relay) otherwise import litellm simultaneously on
    # two executor threads, and the importlib race surfaces as KeyError('litellm')
    # -> agent_init_error -> every turn 503s until a lucky reboot.
    import litellm  # noqa: F401, PLC0415

    try:
        agent = await loop.run_in_executor(None, _build)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[clio-agent-gact] deferred agent init failed ({exc!r}); "
            "POST /messages will keep returning 503.",
            flush=True,
        )
        app.state.agent_init_error = repr(exc)
        return

    # _set_app_arc must run before the boot fold (reads app.state.arc) and before ready.
    _set_app_arc(app, agent.arc)
    # #975: emit the boot `sandbox.state` conformance event (owner module owns the logic).
    from clio_agent.runtime import sandbox  # noqa: PLC0415

    sandbox.emit_boot_state_event(app, getattr(app.state, "sandbox", None))
    # #971: boot-fold the artifact registry off-loop before ready (defects 2 + 1b; owner helper).
    from clio_agent.gact.artifacts import registry_boot  # noqa: PLC0415

    if not await registry_boot.boot_fold_artifact_registry_offloop(app, loop):
        return  # wedged store — agent stays unready with a typed agent_init_error
    app.state.agent = agent

    # #972: enforce the CAS store byte budget across every workspace at boot (off-loop,
    # #1001 cadence — the registry is now folded, so the reachability scan is ready).
    # Best-effort: a GC failure never blocks the agent coming ready.
    async def _boot_cas_gc() -> None:
        try:
            from clio_agent.gact.artifacts.cas_gc import run_boot_cas_gc  # noqa: PLC0415

            await loop.run_in_executor(None, run_boot_cas_gc, app)
        except Exception as exc:  # noqa: BLE001 — boot CAS GC is best-effort
            logger.warning("cas boot gc skipped reason=cas_boot_gc_failed error=%r", exc)

    app.state.cas_boot_gc_task = asyncio.create_task(_boot_cas_gc())

    # Install the deferred permission gate + tool observer now that we
    # know an agent exists to gate. See build_app for why these aren't
    # installed at construction time.
    try:
        _install_tool_runtime_hooks(app)
    except Exception as exc:  # noqa: BLE001 - logged reason=tool_runtime_hooks_install_failed + state flag set (see below)
        # HIGHEST-SEVERITY silent fallback (#772): a failed install leaves the
        # server running WITHOUT a permission gate or tool observer — tools would
        # execute ungated and unobserved. Never swallow: flip the flag, capture the
        # error, and log a structured reason so /v1/health and the trace show it.
        app.state.tool_hooks_installed = False
        app.state.tool_hooks_install_error = repr(exc)
        logger.error(
            "tool runtime hooks failed to install "
            "reason=tool_runtime_hooks_install_failed error=%r",
            exc,
        )

    print("[clio-agent-gact] agent ready.", flush=True)


# #1081 (no-accretion): the scheduler tick + fire runtime lives in its owner module
# clio_agent.gact.scheduler_runtime. Re-exported here so the boot _lifespan hook and
# the scheduler tests keep importing them from clio_agent.gact.app, and so the
# monkeypatch of _turn_start_background_user_turn (resolved through THIS module at fire
# time) still steers the staging path.
from clio_agent.gact.scheduler_runtime import (  # noqa: E402,F401 - re-exported for tests + _lifespan
    _fire_schedule,  # noqa: F401
    _scheduler_tick,
    _scheduler_tick_once,  # noqa: F401
    _seconds_until_next_minute,  # noqa: F401
)


class ARCLike(Protocol):
    """Structural interface for the ARC reference /v1/memory/stats
    pulls from. Real ``ARCMemory`` matches it; tests pass a fake.

    ``get_cache_stats`` returns a dict with ``hits`` / ``misses`` /
    ``hit_rate`` / ``capacity`` (see ``ARCMemory.get_cache_stats``).
    """

    def get_cache_stats(self) -> dict[str, Any]:  # pragma: no cover
        ...


def build_app(
    sessions_path: Optional[Path] = None,
    agent: Optional[AgentLike] = None,
    arc: Optional[ARCLike] = None,
) -> FastAPI:
    """Construct the FastAPI app.

    Kept as a factory (not a module-level ``app = FastAPI()``) so
    tests can build fresh instances without singleton state; the
    module-level ``app`` below is for ``uvicorn
    clio_agent.gact.app:app`` invocations.

    ``sessions_path`` overrides where the session registry persists.
    ``None`` uses the production default (``~/.config/clio-agent/
    sessions.json``); tests pass ``tmp_path / "sessions.json"`` for
    isolation.

    ``agent`` is the ClioAgent-like object driving turns. Left
    ``None`` for builds that only exercise session CRUD without
    actual LM calls — endpoints needing an agent (POST messages, SSE)
    return a structured 503 until one is wired. Production main()
    constructs a real ``ClioAgent`` and passes it here.
    """

    app = FastAPI(
        title="CLIO GACT v0.2",
        version=GACT_BACKEND_VERSION,
        lifespan=_lifespan,
    )

    install_protocol_negotiation(app)

    configure_bearer_auth(app)

    # Browser/WebView origins are explicit: trust_socket must not grant arbitrary
    # sites access. Configure gact.cors.origins; CLIO_GACT_CORS_ORIGINS remains the
    # compatibility fallback.
    # Must precede CORSMiddleware; see install_error_envelope for why.
    install_error_envelope(app)

    allow_origins = _gact_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    # Initialise state eagerly in case the caller skips the lifespan
    # context (TestClient normally runs it, but older FastAPI + some
    # test-utility paths don't).
    app.state.started_at = time.time()
    session_store_path = sessions_path if sessions_path is not None else _default_store_path()
    app.state.sessions = SessionStore(path=session_store_path)
    app.state.agent = agent  # may be None; POST message checks before using
    _set_app_arc(app, arc)  # arc may be None; /v1/memory/stats returns zeros then
    prompt_write_root = session_store_path.parent / "prompts"
    app.state.prompt_registry = PromptRegistry(
        sources=[
            PromptSource("global", prompt_write_root),
            PromptSource("workspace", Path.cwd() / ".clio" / "prompts"),
        ],
        write_root=prompt_write_root,
    )
    app.state.memory_events = {}
    app.state.command_audit = []
    # per-session pub/sub. POST /messages
    # publishes; /v1/sessions/{sid}/events subscribers consume.
    app.state.bus = EventBus()
    initialize_a2ui_store(app, session_store_path.parent)
    app.state.semantic_trace_detail_level = _semantic_trace_detail_level()
    app.state.semantic_trace_backend = build_trace_backend(
        session_store_path.parent / "semantic_traces"
    )
    provenance_wiring.wire_artifact_provenance(
        app, session_store_path.parent / "artifact_provenance"
    )
    # ARC-as-source: the sink has NO arc live_consumer. ARC is the SOURCE now —
    # _emit_semantic_event routes each event through arc.record_semantic_event, which
    # folds the observer (on_semantic_event) INSIDE its record and then derives THIS
    # sink. Registering arc.on_semantic_event here too would double-fold; routing the
    # sink back into arc would recurse. So live_consumers stays empty: arc.record ->
    # sink.emit -> {trace, SSE, hooks} (no arc), a strict one-way derivation.
    app.state.semantic_event_sink = SemanticEventSink(
        bus=app.state.bus,
        trace_backend=app.state.semantic_trace_backend,
        artifact_backend=app.state.artifact_provenance_backend,
        detail_level=app.state.semantic_trace_detail_level,
        live_consumers=None,
    )
    provenance_wiring.install_session_lifecycle_observer(app)
    # (ARC's arc.op op-logger AND highway-derive sink are wired via _set_app_arc
    # whenever app.state.arc is assigned — see _set_app_arc; the highway closure reads
    # app.state.semantic_event_sink at fire-time, so this construction order is fine.)
    # Durable per-session message log (POST /messages writes, GET /messages reads);
    # per-session JSON ledgers so adapter deletion/redeploy preserves transcripts.
    app.state.message_store = MessageStore(path=session_store_path.parent / "messages")
    # #770 C3: bounded eviction-audit trail (init before the resident set).
    init_retention_state(app)
    # #770 C3 / #889: running metrics aggregate, seeded by a streaming parse-and-
    # DISCARD walk so the metrics wire stays byte-identical across a restart WITHOUT
    # pinning every transcript in RAM.
    app.state.metrics_counters = MetricsCounters()
    seed_metrics_counters(app.state.message_store, app.state.metrics_counters)
    # #889: BOUNDED (LRU + byte cap + idle-TTL) resident projection over the store —
    # boots empty (index only), materializes lazily. See gact.resident_ledgers.
    app.state.messages = build_resident_ledger_set(app)
    # cooperative cancellation flags. POST /cancel
    # adds a sid; the POST-message handler checks + clears after the
    # agent returns. Set (not dict) because the flag's presence IS
    # the signal — no payload.
    app.state.cancel_flags = set()
    app.state.cancel_events = {}
    app.state.cancel_attempts = {}
    # per-session context files. Keyed by
    # session_id, each value is an ordered dict of
    # path -> ContextFile dict.
    app.state.context_files_path = session_store_path.parent / "context_files.json"
    app.state.context_files = _load_context_files(app.state.context_files_path)
    # iowarp/clio-agent#331: per-turn context truth frames. These
    # capture what visible transcript/context attachments were
    # retained for a turn, plus model/agent/prompt provenance.
    app.state.context_frames = {}
    # iowarp/clio-agent#369: agent-callable memory tool audit. Tool
    # reads are policy-gated and provenance-bearing so cross-session
    # context is visible after the fact.
    app.state.memory_tool_audit = []
    # per-session pending diffs. Keyed by
    # session_id -> list of {path, unified_diff, status,
    # part_id, message_id}. Status is "pending" until apply/reject
    # flips it.
    app.state.pending_diffs = {}
    # pending permission requests. Flat dict
    # keyed by permission_id so GET /v1/permissions can filter by
    # session cheaply. Each record carries
    # {id, session_id, tool_call, summary, created_at, status,
    #  action, resolved_at}.
    app.state.permissions = {}
    # iowarp/clio-agent#7: per-permission threading.Event so the
    # MCPToolBridge gate (running in a worker thread) can block on
    # the user's response without polling.
    app.state.permission_events = {}
    # iowarp/clio-agent#333: structured ask-user protocol. The
    # orchestrator/backend can publish pending questions; clients
    # answer or cancel them through explicit endpoints.
    app.state.user_questions = {}
    # iowarp/clio-agent#333: retry attempts preserve provenance for
    # retry-with-notes/model flows without mutating the original turn.
    app.state.turn_attempts = {}
    # SPEC §6.11.b permission policies — list, not dict. Backends
    # consult this on every tool call to decide allow/deny/ask at the
    # permission boundary. PUT replaces the whole list.
    app.state.permission_policies_path = session_store_path.parent / "permission_policies.json"
    app.state.permission_policies = _load_permission_policies(app.state.permission_policies_path)
    # iowarp/clio-agent#18: per-session task list (todo-style).
    # Keyed by session_id -> {task_id -> task dict}. In-memory.
    app.state.session_tasks = {}
    # iowarp/clio-agent#3: per-session in-flight turn tasks. POST
    # /messages tracks the asyncio.Task here so /cancel can
    # hard-abort instead of waiting for the cooperative flag check.
    app.state.in_flight_turns = {}
    # #948 S1 (#662): the single owner of in-flight turn-task lifetime (master
    # strong-ref set → no GC-cancellation; app-loop anchored; busy gate; typed
    # shutdown drain). ``in_flight_turns`` stays its per-session view.
    install_turn_runner(app)
    # #948 S2 (#950): in-memory AgentTask projection over the session store, rebuilt
    # from agent-task sessions; feeds events, the task API, and S3+ spawns.
    install_agent_task_registry(app)
    # #1205: bridge every durable MCP TaskRecord write (#1115) to this app's event
    # bus, so the async-processes tray refreshes live instead of polling.
    install_mcp_task_event_publisher(app)
    # #948 S3 (#951): dedicated child-forward pool, sized to the concurrency cap,
    # prevents a waiting parent from starving its children.
    install_agent_task_executor(app)
    app.state.expert_invoker = InProcessExpertInvoker(app)
    relay_wiring.configure_relay_expert_invokers(app)
    # #948 S1: schedule ids deferred because their session was busy at the cron
    # minute; _scheduler_tick_once retries them until the session frees (a coarse
    # cron can't be retried via due_now, which only re-yields on a cron match).
    app.state.deferred_schedules = set()
    # #1035/#1036 (epic #1031 Pillar 2): per-session loop inboxes — the mid-turn
    # wake + user-steer carrier (session_id -> LoopInbox). See gact/loop_inbox.py.
    # #1036 folded the former app.state.deferred_resumes stash here: an ask-user
    # resume that arrives while busy is enqueued as a user_message steer and the
    # idle hook (drain_inbox_to_new_turn) re-drives residual steers into ONE turn.
    app.state.loop_inboxes = {}
    app.state.turn_runner.set_idle_hook(lambda sid: drain_inbox_to_new_turn(app, sid))
    # iowarp/clio-agent#2: per-session ledger of tool calls observed
    # during the in-flight turn. The global tool_observer appends
    # here; _run_turn_in_background drains it post-forward to attach
    # tools_called metadata even when the underlying expert
    # didn't populate ``pred.tools_called`` itself.
    app.state.tool_call_ledger = {}
    # In-flight assistant message/parts emitted from real runtime
    # boundaries before the final assistant message is persisted. This
    # lets SSE clients show tool calls and delegations as they happen.
    app.state.live_assistant_message_ids = {}
    app.state.live_assistant_parts = {}
    app.state.live_assistant_part_keys = {}
    # #767 PR1: the single-writer part-ledger registry (TurnTranscript). No
    # production path opens a turn yet — the turn loop adopts it in PR2/PR3 —
    # but the tool-observer/delegation append helpers already shim into any
    # open ledger, falling back to the legacy dicts above when none is open.
    app.state.turn_transcripts = TurnTranscriptRegistry()

    # iowarp/clio-agent#7 + #2: install process-global hooks on the
    # MCPToolBridge so EVERY expert's tool call routes through our
    # permission gate + telemetry observer.
    #
    # When an agent is already in hand we install eagerly — that's
    # the legacy build_app(agent=X) path tests use. When the caller
    # left agent=None (the production main() flow that defers
    # ClioAgent construction to the lifespan task) we stash the
    # closures on app.state and install them right after the agent
    # finishes constructing — importing clio_agent.tools.execution
    # transitively pulls litellm + dspy (~4 s) and we need build_app
    # to stay cheap enough for gact-tui's 3-second deploy probe.
    #
    # Expose the gate/observer CONSTRUCTORS on app.state so runtime code carved
    # out of this module (#714 decomposition) can build a fresh gate/observer
    # WITHOUT importing _make_permission_gate/_make_tool_observer from app.py
    # (which would reintroduce the no-cycle violation). Callers prefer the
    # already-installed app.state.pending_permission_gate/pending_tool_observer
    # and fall back to these factories — mirroring _call_enabled_external_mcp_tool.
    app.state.make_permission_gate = lambda: _make_permission_gate(app)
    app.state.make_tool_observer = lambda: _make_tool_observer(app)
    # MCP Apps receives the full FastMCP result through a separate private
    # observer. It is intentionally not folded into ``tool_observer`` because
    # that observer writes result evidence into durable traces.
    install_mcp_app_runtime(app)
    # #735 unified-concurrency seam: install the STATELESS tool-runtime resolver
    # once (idempotent). It dispatches on the live turn's ``active_app()`` so N
    # apps in one process each read THEIR OWN ``app.state.pending_*`` hooks — no
    # shared process-global on the in-turn path. Installed unconditionally (both
    # the eager-agent and deferred-construction branches below run turns).
    from clio_agent.gact.runtime.app_state import resolve_tool_runtime  # noqa: PLC0415
    from clio_agent.tools.execution import set_tool_runtime_resolver  # noqa: PLC0415

    set_tool_runtime_resolver(resolve_tool_runtime)
    # #1035: install the injected loop-inbox drain (both boot branches run turns);
    # resolve_tool_runtime folds it into ToolRuntimeHooks (acyclic edge preserved).
    app.state.pending_loop_inbox_drain = _make_loop_inbox_drain(app)
    if agent is not None:
        try:
            _install_tool_runtime_hooks(app)
        except Exception as exc:  # noqa: BLE001 - logged reason=tool_runtime_hooks_install_failed + state flag set (see below)
            # HIGHEST-SEVERITY silent fallback (#772): see the sibling handler in
            # _finish_agent_init. A swallowed install failure = an ungated,
            # unobserved tool surface. Fail loud: flip the flag, capture the
            # error, and log a structured reason.
            app.state.tool_hooks_installed = False
            app.state.tool_hooks_install_error = repr(exc)
            logger.error(
                "tool runtime hooks failed to install "
                "reason=tool_runtime_hooks_install_failed error=%r",
                exc,
            )
    else:
        # Deferred-agent boot (production main()): hooks are installed later by
        # _construct_agent_async. ``None`` = not-yet-determined; ``False`` is
        # reserved EXCLUSIVELY for an install failure so /v1/health never
        # reports a normal startup window as an ungated tool surface (#772).
        app.state.tool_hooks_installed = None
        app.state.pending_cancellation_checker = _make_cancellation_checker(app)
        app.state.pending_permission_gate = _make_permission_gate(app)
        app.state.pending_tool_observer = _make_tool_observer(app)
        # P2.3: synthesize/modify interceptor + PostToolUse producer, so a turn driven
        # before _construct_agent_async runs still has them wired.
        from clio_agent.gact.hooks import make_post_tool_hook, pre_tool_interceptor  # noqa: PLC0415

        app.state.pending_tool_interceptor = pre_tool_interceptor
        app.state.pending_post_tool = make_post_tool_hook(app)

    # P2.2 #1070: install the ONE hook dispatcher so PreToolUse / UserPromptSubmit /
    # Stop / SemanticEvent events route to the declarative hooks config
    # (<user_config>/hooks.json + <cwd>/.clio/hooks.json). Tests pre-install their
    # own dispatcher; we only build a default when nothing is currently wired so the
    # test-side dispatcher stays. Metadata lands on app.state for /v1/capabilities
    # (the same wiring shape the deleted runtime registry used — no new store).
    try:
        from clio_agent.gact.hooks import (
            build_hook_dispatcher,
            get_global_dispatcher,
            install_global_dispatcher,
        )

        dispatcher = get_global_dispatcher()
        if dispatcher is None:
            dispatcher = build_hook_dispatcher()
            install_global_dispatcher(dispatcher)
        app.state.runtime_hook_registry_metadata = (
            dispatcher.metadata() if hasattr(dispatcher, "metadata") else {}
        )
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001 - dispatcher-metadata unavailability recorded in app.state
        app.state.runtime_hook_registry_metadata = {
            "backend": "unavailable",
            "enabled": False,
            "error": "failed_to_initialize",
        }

    # live LM config — what the TUI configured
    # us with. Distinct from boot-time env because PUT /providers/lm
    # rebuilds the agent + DSPy config in-place.
    app.state.lm_config = None
    app.state.lm_config_status = {"state": "idle"}
    app.state.lm_config_task = None
    app.state.lm_studio_owned_instance = None
    # Per-app provider-profile registry (design §3.4 / §9 step 4). An immutable
    # snapshot mapping profile-id -> LMSpec with one "default" entry, seeded from
    # the same boot config the agent builds from (spec_from_config of
    # load_config_from_env). Per-app so the two-app test topology holds two
    # independent stores instead of racing one process-global. Additive/shadow:
    # nothing routes LM resolution through it yet. load_config_from_env may raise
    # for a misconfigured cloud provider (missing key); that must not fail app
    # construction (baseline: the deferred agent build tolerates it), so we fall
    # back to the plain provider-default spec and let the deferred build surface
    # the real error.
    from clio_agent.config import LMProviderConfig, load_config_from_env
    from clio_agent.gact.providers.profile_store import ProviderProfileStore
    from clio_agent.providers.lm_spec import spec_from_config

    try:
        _boot_cfg = load_config_from_env()
    except Exception:  # noqa: BLE001 - misconfig must not break app construction
        _boot_cfg = LMProviderConfig()
    app.state.provider_profiles = ProviderProfileStore.seed(spec_from_config(_boot_cfg))
    # workspaces store. Persisted alongside
    # sessions; seeds a default workspace if none exist so the TUI
    # always has something to render.
    app.state.workspaces = WorkspaceStore(
        path=(sessions_path.parent / "workspaces.json")
        if sessions_path is not None
        else _ws_default_store_path()
    )
    # iowarp/clio-agent#19: dynamic agent registry. Persists user-
    # registered Tier-2 specialists alongside sessions/workspaces;
    # built-ins always take precedence on id clash (rejected at
    # the HTTP layer).
    from clio_agent.gact.user_agents import (
        UserAgentStore,
    )
    from clio_agent.gact.user_agents import (
        _default_store_path as _ua_default,
    )

    app.state.user_agents = UserAgentStore(
        path=(sessions_path.parent / "agents.json") if sessions_path is not None else _ua_default()
    )
    # iowarp/clio-agent#21: scheduled turns store + tick task.
    from clio_agent.gact.scheduler import ScheduleStore as _SchedStore

    app.state.schedules = _SchedStore(
        path=(sessions_path.parent / "schedules.json") if sessions_path is not None else None
    )
    initialize_session_defaults(app, sessions_path)
    app.state.scheduler_task = None
    # iowarp/clio-agent#22: shared session tokens.
    app.state.shared_tokens = {}

    # ---- /v1/health + /v1/capabilities + /v1/capability-gaps + /v1/metrics ----
    # + /v1/memory/stats: the read-only system/observability surface is owned by
    # routes/system.py and registered below via register_system_routes(app, deps)
    # once ``deps`` is built. The static capability/metrics catalogs they project
    # live in runtime/capabilities.py (shared with the message-turn streaming
    # path here); the wire/limit constants live in runtime/constants.py.

    # ---- 501 stubs for the rest of the surface ---------------------------
    # Every route in the v0.2 contract that we haven't wired yet
    # returns the structured error envelope from above. Matches the
    # shape v0.2 clients expect, while honestly reporting that the
    # backend doesn't yet implement the endpoint.

    # ---- /v1/prompts (CLIO prompt registry) ------------------------------

    def _prompt_workspace_root(workspace_id: str = "", session_id: str = "") -> Path:
        wid = workspace_id
        if session_id:
            sess = app.state.sessions.get(session_id)
            if sess is not None:
                wid = wid or str(getattr(sess, "workspace_id", "") or "")
        if wid:
            ws = app.state.workspaces.get(wid)
            if ws is not None:
                root_path = str(getattr(ws, "root_path", "") or "")
                if root_path:
                    return Path(root_path).expanduser()
        return Path.cwd()

    def _active_prompt_pack_path(session_id: str = "") -> Path | None:
        if not session_id:
            return None
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return None
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return None
        raw = str(metadata.get("active_expert_pack_path") or "").strip()
        return Path(raw).expanduser() if raw else None

    def _prompt_sources_for_request(
        *,
        session_id: str = "",
        workspace_id: str = "",
    ) -> list[PromptSource]:
        cwd = _prompt_workspace_root(workspace_id=workspace_id, session_id=session_id)
        sources = [
            PromptSource("global", prompt_write_root),
        ]
        for pack in discover_expert_packs(cwd=cwd):
            prompt_root = pack.root / "prompts"
            if prompt_root.is_dir():
                sources.append(PromptSource(f"{pack.scope}_pack", prompt_root))
        sources.append(PromptSource("workspace", cwd / ".clio" / "prompts"))
        if session_id:
            active_blueprint_path = _active_session_agent_blueprint_path(session_id)
            active_blueprint_id = _active_session_agent_blueprint_id(session_id)
            active_blueprint_root = active_blueprint_path
            if active_blueprint_root is None and active_blueprint_id:
                active = next(
                    (
                        row
                        for row in discover_agent_blueprints(cwd=cwd)
                        if row.id == active_blueprint_id
                    ),
                    None,
                )
                active_blueprint_root = active.root if active is not None else None
            if active_blueprint_root is not None and (active_blueprint_root / "prompts").is_dir():
                sources.append(
                    PromptSource("session_agent_blueprint", active_blueprint_root / "prompts")
                )
        active_pack_path = _active_prompt_pack_path(session_id)
        if active_pack_path is not None and (active_pack_path / "prompts").is_dir():
            sources.append(PromptSource("session_pack", active_pack_path / "prompts"))
        if session_id:
            sources.append(
                PromptSource("session", prompt_write_root.parent / "session-prompts" / session_id)
            )
        return sources

    def _prompt_write_root_for_request(
        *,
        scope: str,
        session_id: str = "",
        workspace_id: str = "",
    ) -> Path:
        if scope == "session":
            if not session_id:
                raise ValueError("session_id is required for session prompt writes")
            return prompt_write_root.parent / "session-prompts" / session_id
        if scope == "workspace":
            cwd = _prompt_workspace_root(workspace_id=workspace_id, session_id=session_id)
            return cwd / ".clio" / "prompts"
        if scope in {"global", "user", ""}:
            return prompt_write_root
        raise ValueError("scope must be global, workspace, or session")

    def _prompt_registry_for_request(
        *,
        session_id: str = "",
        workspace_id: str = "",
        write_scope: str = "global",
    ) -> PromptRegistry:
        sources = _prompt_sources_for_request(session_id=session_id, workspace_id=workspace_id)
        return PromptRegistry(
            sources=sources,
            builtins=app.state.prompt_registry._builtins(),
            write_root=_prompt_write_root_for_request(
                scope=write_scope,
                session_id=session_id,
                workspace_id=workspace_id,
            ),
        )

    app.state.prompt_registry_for_request = _prompt_registry_for_request

    def _prompt_agent_overlay_for_request(session_id: str = "") -> dict[str, Any]:
        if not session_id:
            return {}
        overlay = _session_agent_overlay(session_id)
        agents = overlay.get("agents") if isinstance(overlay, Mapping) else None
        if not isinstance(agents, Mapping):
            return {}
        rows: list[dict[str, Any]] = []
        prompt_fields = {
            "system_prompt",
            "prompt_id",
            "prompt_profile",
            "default_provider",
            "default_model",
        }
        for agent_id, raw_patch in sorted(agents.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_patch, Mapping):
                continue
            fields = sorted(str(key) for key in raw_patch if str(key) in prompt_fields)
            if not fields:
                continue
            rows.append(
                {
                    "agent_id": str(agent_id),
                    "fields": fields,
                    "has_system_prompt": bool(str(raw_patch.get("system_prompt") or "").strip()),
                    "prompt_id": str(raw_patch.get("prompt_id") or "").strip(),
                    "prompt_profile": str(raw_patch.get("prompt_profile") or "").strip(),
                    "default_provider": str(raw_patch.get("default_provider") or "").strip(),
                    "default_model": str(raw_patch.get("default_model") or "").strip(),
                    "source": "session_agent_overlay",
                    "session_id": session_id,
                }
            )
        return {
            "session_id": session_id,
            "source": "session_agent_overlay",
            "agents": rows,
        }

    def _prompt_render_context_for_request(
        *,
        session_id: str = "",
        workspace_id: str = "",
    ) -> dict[str, str]:
        context = _prompt_render_context(app)
        if session_id or workspace_id:
            try:
                agents = [
                    row
                    for row in _agent_rows(session_id=session_id, workspace_id=workspace_id)
                    if row.enabled
                ]
                by_parent: dict[str, list[AgentDef]] = {}
                for agent in agents:
                    by_parent.setdefault(agent.parent_id or "", []).append(agent)

                def render_tree(parent_id: str = "", depth: int = 0) -> list[str]:
                    lines: list[str] = []
                    for agent in sorted(
                        by_parent.get(parent_id, []), key=lambda row: (row.tier, row.id)
                    ):
                        indent = "  " * depth
                        detail = f" - {agent.description}" if agent.description else ""
                        lines.append(f"{indent}- {agent.id}: {agent.title}{detail}")
                        lines.extend(render_tree(agent.id, depth + 1))
                    return lines

                context["agents.available_tree"] = (
                    "\n".join(render_tree()) or "(no enabled experts)"
                )
                context["agents.available_flat"] = (
                    "\n".join(
                        f"- {agent.id}: {agent.title}"
                        for agent in sorted(agents, key=lambda row: row.id)
                    )
                    or "(no enabled experts)"
                )
            except Exception:  # noqa: BLE001,S110 - enabled-experts prompt hint is best-effort; planner proceeds without it
                pass
            if session_id:
                pack_id = ""
                blueprint_id = ""
                agent_id = ""
                sess = app.state.sessions.get(session_id)
                if sess is not None:
                    agent_id = _session_agent_id(sess)
                    metadata = getattr(sess, "metadata", {}) or {}
                    if isinstance(metadata, Mapping):
                        pack_id = str(metadata.get("active_expert_pack_id") or "").strip()
                        blueprint_id = str(metadata.get("active_agent_blueprint_id") or "").strip()
                context["session.active_pack"] = pack_id or "(no active expert pack)"
                context["session.active_agent_blueprint"] = (
                    blueprint_id or "(no active agent blueprint)"
                )
                try:
                    commands = [
                        f"- {row.get('id')}: {row.get('description') or row.get('title')}"
                        for row in _planner_command_rows(
                            app,
                            _resolve_runtime_dynamic_agent_bound,
                            agent_id=agent_id,
                            cwd=_command_cwd_for_request(app, session_id),
                            session_id=session_id,
                        )
                    ]
                    context["commands.agent_invocable"] = (
                        "\n".join(commands) or "(no agent-invocable commands)"
                    )
                except Exception as exc:  # noqa: BLE001 - enrichment; keep the base list
                    # No silent fallback: record WHY the agent-scoped command
                    # enrichment was skipped so an arity/resolver break is
                    # queryable in the trace instead of silently reverting the
                    # render context to the un-scoped base command list.
                    trace.event(
                        "PROMPT-CTX",
                        "agent-invocable command enrichment failed for agent %r "
                        "(session %r): %s; rendering un-scoped base command list",
                        agent_id,
                        session_id,
                        exc,
                    )
        return context

    # ---- /v1/sessions CRUD + delete -----------------------------------
    # Session create/list/get/patch + permission-gated delete are owned by
    # routes/sessions.py and registered below via register_sessions_routes(
    # app, deps); the delete cascade (messages/context-files/ARC release)
    # travels on ``deps``.

    # ---- /v1/sessions/{sid}/context/* (ARC live-context plane) -------
    # The session context compartment policy + the live ARC context-plane
    # routes (state/ops/compact/search) are owned by routes/context.py and
    # registered below (after ``deps`` is built); the segment-token arithmetic
    # + window resolution remain in runtime/context_tokens.py (shared with the
    # expert forward path).

    # ---- /v1/sessions/{sid}/undo + .../rewind -------------------------
    # Transcript rollback (undo/rewind) is owned by routes/sessions.py and
    # registered below via register_sessions_routes(app, deps); the ledger
    # replace + destructive-action guard travel on ``deps``.

    # ---- /v1/permissions (BBB23) + /v1/policies (SPEC §6.11.b) --------
    # Permission-request ledger CRUD + declarative permission-policy CRUD are
    # owned by routes/permissions.py; registered once below alongside the other
    # register_<concern>_routes factories (after ``deps`` is built).

    # ---- POST /v1/sessions/{sid}/fork (BBB26) -------------------------
    # Forking a session + its messages into a fresh child is owned by
    # routes/sessions.py and registered below via register_sessions_routes(
    # app, deps); the ledger replace travels on ``deps``.

    # ---- /v1/providers (#15) + /v1/providers/lm ---
    # The LM-provider catalog (list/detail/auth/models/handshake) and the
    # runtime LM-bind routes (get/put/wait LM config, incl. the dspy.settings
    # + env snapshot/restore bind closures) are owned by routes/providers.py and
    # registered below via ``register_providers_routes(app, deps)`` once ``deps``
    # is built. The bind reaches the agent-rebuild hooks (install-tool-runtime-
    # hooks / clear-session-model-refs) through ``deps``.

    # ---- /v1/mcp/servers (#13) ---------------------------------------
    # The MCP server registry + dispatch routes (servers list/detail/install/
    # call/reconnect/uninstall + tools/resources/prompts + handshake) are owned
    # by routes/mcp.py; registered below via register_mcp_routes(app, deps).

    # ---- /v1/sessions/{sid}/compact (Codex/CC parity) -----------------
    # Transcript compaction into an evidence-preserving compact memory is
    # owned by routes/sessions.py and registered below via
    # register_sessions_routes(app, deps); the deterministic evidence index
    # + ledger replace travel on ``deps``.

    # ---- /v1/sessions/{sid}/schedules (#21) --------------------------
    # Scheduled-turn CRUD (list/add/delete) is owned by routes/schedules.py and
    # registered below via register_schedules_routes(app, deps) once ``deps`` is
    # built; the scheduler tick task (above) owns the actual firing.

    # ---- /v1/sessions/{sid}/export + /v1/sessions/import (#16) -------
    # Portable session export + import round-trip are owned by
    # routes/sessions.py and registered below via register_sessions_routes(
    # app, deps).

    # ---- GET /v1/sessions/{sid}/messages/search (BBB27) ---------------
    # The message ledger surface -- search, the turn-entry POST, the
    # list/get reads and the message-delete routes -- is owned by
    # routes/messages.py and registered below via register_messages_routes(
    # app, deps); the destructive-action guard, ledger replace,
    # background-turn entrypoint, active-model ref + override error, and
    # the agent-not-available error all travel on ``deps``.

    # ---- Ask-user and retry protocol (#333) --------------------------
    # The user-question ledger (list/create/answer/cancel) + the turn
    # retry routes (attempts list + messages/{id}/retry) are owned by
    # routes/sessions.py and registered below via register_sessions_routes(
    # app, deps). Answering a resume-on-answer question + executing a retry
    # both drive a turn through ``deps.start_background_user_turn`` (the
    # thin ``build_app`` wrapper around the turn engine, defined just below).
    def _start_background_user_turn(
        sid: str,
        sess: Session,
        user_text: str,
        *,
        request_parts: Optional[list[Part]] = None,
        metadata: Optional[dict[str, Any]] = None,
        prev_status: str = "idle",
        turn_agent_id: str = "",
    ) -> Message:
        """Stage + drive a user turn (thin ``build_app`` wrapper, #714).

        The engine moved to :func:`clio_agent.gact.turn._start_background_user_turn`
        (single source). ``build_app``'s in-closure callers + the route factories
        (via ``GactDeps.start_background_user_turn``) reach it through this wrapper
        so they need not thread ``app`` explicitly; behavior is unchanged.
        """
        return _turn_start_background_user_turn(
            app,
            sid,
            sess,
            user_text,
            request_parts=request_parts,
            metadata=metadata,
            prev_status=prev_status,
            turn_agent_id=turn_agent_id,
        )

    # ---- POST /v1/sessions/{sid}/cancel (BBB20) -----------------------
    # Best-effort cooperative cancel of an in-flight turn is owned by
    # routes/sessions.py and registered below via register_sessions_routes(
    # app, deps); the cancellation-attempt summary travels on ``deps``.

    # ---- POST /v1/sessions/{sid}/messages (BBB9) + reads (BBB10) -----
    # The turn-entry POST, the list/get reads and the message-delete
    # routes are owned by routes/messages.py and registered below via
    # register_messages_routes(app, deps) (see the search pointer above).

    # ---- /v1/agents catalog (BBB10) + dynamic registry (#19) ---------
    #
    # Effective-agent resolution (blueprint rows + MCP tool-gating + capability
    # refs + default-blueprint fallback) is owned by
    # ``clio_agent.gact.agents.resolution``; ``/v1/agents`` (``_agent_rows``) and
    # the runtime turn path share the ONE ``_runtime_active_agent_blueprint_rows``
    # seam so they can never disagree (#770 C1). The build_app-local closures kept
    # here are the thin app-binding wrappers ``deps`` needs (1-arg seams that bind
    # ``app``) plus the metadata-only readers that are deliberately distinct from
    # the fallback-aware runtime readers.

    def _agent_with_capability_refs_bound(agent_def: AgentDef) -> AgentDef:
        """Bind ``app`` for the 1-arg capability-ref seam ``deps`` + routes use."""

        return _agent_with_capability_refs(app, agent_def)

    def _workspace_catalog_cwd(workspace_id: str = "", session_id: str = "") -> Path | None:
        wid = workspace_id
        if session_id:
            sess = app.state.sessions.get(session_id)
            if sess is not None:
                wid = wid or str(getattr(sess, "workspace_id", "") or "")
        if not wid:
            return None
        ws = app.state.workspaces.get(wid)
        if ws is None:
            return None
        root_path = str(getattr(ws, "root_path", "") or "")
        return Path(root_path).expanduser() if root_path else None

    def _active_session_agent_blueprint_id(session_id: str = "") -> str:
        if not session_id:
            return ""
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return ""
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return ""
        return str(metadata.get("active_agent_blueprint_id") or "").strip()

    def _active_session_agent_blueprint_path(session_id: str = "") -> Path | None:
        if not session_id:
            return None
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return None
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return None
        raw = str(metadata.get("active_agent_blueprint_path") or "").strip()
        return Path(raw).expanduser() if raw else None

    def _agent_blueprint_activation_metadata(
        *,
        blueprint_wire: Mapping[str, Any],
        install_root: Path | None,
        scope: str,
    ) -> dict[str, str]:
        install = read_install_metadata(install_root) if install_root is not None else {}
        return {
            "active_agent_blueprint_id": str(blueprint_wire.get("id") or ""),
            "active_agent_blueprint_name": str(
                blueprint_wire.get("name")
                or blueprint_wire.get("display_name")
                or blueprint_wire.get("title")
                or ""
            ),
            "active_agent_blueprint_version": str(blueprint_wire.get("version") or ""),
            "active_agent_blueprint_scope": scope,
            "active_agent_blueprint_definition_path": str(
                blueprint_wire.get("definition_path") or ""
            ),
            "active_agent_blueprint_source": str(install.get("source") or ""),
            "active_agent_blueprint_source_kind": str(install.get("source_kind") or ""),
            "active_agent_blueprint_ref": str(install.get("ref") or ""),
            "active_agent_blueprint_commit": str(install.get("commit") or ""),
            "active_agent_blueprint_checksum": str(install.get("checksum") or ""),
            "active_agent_blueprint_installed_at": str(install.get("installed_at") or ""),
        }

    def _session_agent_overlay(session_id: str = "") -> dict[str, Any]:
        if not session_id:
            return {}
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return {}
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return {}
        overlay = metadata.get("agent_blueprint_overlay")
        return dict(overlay) if isinstance(overlay, Mapping) else {}

    def _base_session_agent_blueprint_rows(
        session_id: str = "",
        workspace_id: str = "",
    ) -> list[AgentDef]:
        if not session_id:
            return []
        cwd = _workspace_catalog_cwd(workspace_id=workspace_id, session_id=session_id)
        active_blueprint_id = _active_session_agent_blueprint_id(session_id)
        active_blueprint_path = _active_session_agent_blueprint_path(session_id)
        if active_blueprint_path is not None:
            return load_agent_blueprint_path(active_blueprint_path, scope="session")
        if active_blueprint_id:
            return load_agent_blueprints(cwd=cwd, blueprint_id=active_blueprint_id)
        return []

    def _apply_agent_overlay_rows(
        rows: list[AgentDef],
        overlay: Mapping[str, Any],
        *,
        session_id: str = "",
    ) -> list[AgentDef]:
        agents = overlay.get("agents") if isinstance(overlay, Mapping) else None
        if not isinstance(agents, Mapping):
            return rows
        patchable = _agent_overlay_patchable_fields()
        out: list[AgentDef] = []
        for row in rows:
            raw_patch = agents.get(row.id)
            if not isinstance(raw_patch, Mapping):
                out.append(row)
                continue
            update = {key: value for key, value in raw_patch.items() if key in patchable}
            metadata = {
                **row.metadata,
                "agent_blueprint_overlay": {
                    "session_id": session_id,
                    "fields": sorted(update),
                    "status": "applied",
                },
            }
            out.append(row.model_copy(update={**update, "metadata": metadata}))
        return out

    def _apply_session_agent_overlay(rows: list[AgentDef], session_id: str = "") -> list[AgentDef]:
        overlay = _session_agent_overlay(session_id)
        return _apply_agent_overlay_rows(rows, overlay, session_id=session_id)

    def _agent_rows(session_id: str = "", workspace_id: str = "") -> list[AgentDef]:
        """Resolve the effective agent catalog ``GET /v1/agents`` renders.

        Delegates the active-blueprint branch to the shared
        :func:`clio_agent.gact.agents.resolution._runtime_active_agent_blueprint_rows`
        seam (dispatched through the module so a monkeypatch of that ONE function
        is honoured identically by this route and the runtime turn path), so the
        list a client sees and the agents that actually execute never diverge
        (#770 C1). Only when no blueprint resolves does it fall back to the
        builtin/user/skill/expert-pack hierarchy.
        """

        cwd = _workspace_catalog_cwd(workspace_id=workspace_id, session_id=session_id)
        prompt_registry = _prompt_registry_for_request(
            session_id=session_id,
            workspace_id=workspace_id,
        )
        rows = _resolution._runtime_active_agent_blueprint_rows(
            app,
            session_id=session_id,
            workspace_id=workspace_id,
            prompt_registry=prompt_registry,
        )
        if rows:
            return rows
        active_pack_id = _runtime_active_session_expert_pack_id(app, session_id)
        active_pack_path = _runtime_active_session_expert_pack_path(app, session_id)
        explicit_session_rows = (
            load_expert_pack_path(active_pack_path, scope="session")
            if active_pack_path is not None
            else []
        )
        rows = (
            _builtin_agents()
            + [AgentDef(**row.to_wire()) for row in app.state.user_agents.list()]
            + load_expert_packs(cwd=cwd, pack_id=active_pack_id)
            + explicit_session_rows
        )
        return [
            _apply_prompt_registry_to_agent(
                app,
                _agent_with_capability_refs(app, row),
                prompt_registry=prompt_registry,
            )
            for row in validate_expert_hierarchy(_merge_agent_def_rows(rows))
        ]

    def _resolve_runtime_dynamic_agent_bound(
        agent_id: str,
        *,
        session_id: str = "",
        workspace_id: str = "",
        prompt_registry: PromptRegistry | None = None,
    ) -> "AgentDef | None":
        """Bind ``app`` for the 1-arg overlay-aware resolver seam ``deps`` carries.

        Dispatches through the ``resolution`` module so both this seam (command
        dispatch / planner-command filter) and the runtime turn path share the ONE
        unified resolver -- the divergent build_app shadow is gone (#770 C1).
        """

        return _resolution._resolve_runtime_dynamic_agent(
            app,
            agent_id,
            session_id=session_id,
            workspace_id=workspace_id,
            prompt_registry=prompt_registry,
        )

    # ---- /v1/agent-blueprints/* + /v1/expert-packs/* lifecycle + session
    # blueprint activation (iowarp/clio-agent#663) -----------------------
    # Blueprint source registry, install/update/delete engine, MCP-descriptor
    # enable, and the session-scoped get/set-active-blueprint routes are owned
    # by routes/blueprints.py and registered below via
    # ``register_blueprints_routes(app, deps)`` once ``deps`` is built. The
    # expert-pack routes are thin aliases of the blueprint lifecycle (one engine,
    # ``kind``-distinguished). The set-active route reaches the activation-metadata
    # builder (and the metadata-only active-id reader) through ``deps``.

    # ---- /v1/expert-packs/* discovery + session attachment ----------------
    # The expert-pack discovery (list/get/validate) and session attachment
    # (get/set active pack) routes are owned by routes/expert_packs.py and
    # registered below via ``register_expert_packs_routes(app, deps)`` once
    # ``deps`` is built. (Pack install/update/delete are blueprint-engine
    # aliases owned by routes/blueprints.py.)

    # ---- /v1/agents/* registry CRUD + extract + /v1/sessions/{sid}/agent-overlay ---
    # The Tier-2 agent registry (list/get/create/update/delete + extract) and the
    # session agent-overlay routes (get/put/export) are owned by routes/agents.py
    # and registered below via ``register_agents_routes(app, deps)`` once ``deps``
    # is built. They reach the shared row-resolution closures (``agent_rows``/
    # ``agent_with_capability_refs``/``base_session_agent_blueprint_rows``/
    # ``apply_agent_overlay_rows``/``prompt_registry_for_request``) plus the
    # destructive-action guard through ``deps``.

    # Cross-concern seam (#714): built once and threaded to every extracted
    # ``register_<concern>_routes(app, deps)`` factory so moved handlers reach
    # shared ``build_app``-local helpers via ``deps`` rather than closing over
    # them. Built here, after every closure it carries is defined. Keep minimal
    # — add a field only when a moved handler needs it.
    deps = GactDeps(
        guard_direct_destructive_action=_guard_direct_destructive_action,
        apply_edit_to_disk=_apply_edit_to_disk,
        flush_context_files=_flush_context_files,
        prompt_registry_for_request=_prompt_registry_for_request,
        prompt_agent_overlay_for_request=_prompt_agent_overlay_for_request,
        prompt_render_context_for_request=_prompt_render_context_for_request,
        active_session_agent_blueprint_id=_active_session_agent_blueprint_id,
        agent_blueprint_activation_metadata=_agent_blueprint_activation_metadata,
        agent_rows=_agent_rows,
        agent_with_capability_refs=_agent_with_capability_refs_bound,
        base_session_agent_blueprint_rows=_base_session_agent_blueprint_rows,
        apply_agent_overlay_rows=_apply_agent_overlay_rows,
        append_session_message=_append_session_message,
        delete_session_messages=_delete_session_messages,
        blueprint_runner_for_agent=_blueprint_runner_for_agent,
        resolve_runtime_dynamic_agent=_resolve_runtime_dynamic_agent_bound,
        start_background_user_turn=_start_background_user_turn,
        delete_session_context_files=_delete_session_context_files,
        release_session_arc=_release_session_arc,
        replace_session_messages=_replace_session_messages,
        cancellation_attempt_summary=_cancellation_attempt_summary,
        active_lm_model_ref=_active_lm_model_ref,
        unsupported_model_ref_error=_unsupported_model_ref_error,
        agent_not_available_error=_agent_not_available_error,
        ask_user_resume_text=_ask_user_resume_text,
        compact_exact_evidence_index=_compact_exact_evidence_index,
        install_tool_runtime_hooks=_install_tool_runtime_hooks,
        clear_session_model_refs=_clear_session_model_refs,
    )

    # ---- /v1/sessions/* lifecycle + ask-user/retry -------------------
    # Session CRUD/delete, rollback (undo/rewind), fork, export/import,
    # compaction, cancel, the user-question ledger and the turn-retry routes
    # are owned by routes/sessions.py. The fork/answer/retry routes drive a
    # background turn through ``deps.start_background_user_turn``; the ledger
    # replace + delete cascade, model-ref errors, evidence
    # index and resume text travel on ``deps``.
    register_sessions_routes(app, deps)
    register_session_defaults_routes(app)

    # ---- /v1/sessions/{sid}/messages + /v1/messages (BBB9/BBB10/BBB27) ---
    # The session message ledger -- the turn-entry POST, the list/get reads,
    # substring search and both message-delete routes -- is owned by
    # routes/messages.py. The turn-entry POST kicks a background turn through
    # ``deps.start_background_user_turn``; the destructive-action guard, ledger
    # replace, active-model ref + override error and the agent-not-available
    # error travel on ``deps``.
    register_messages_routes(app, deps)
    register_a2ui_routes(app, deps)

    # ---- /v1/agent-tasks + /v1/sessions/{sid}/agent-tasks (#948 S2 / #950) ----
    # The AgentTask projection read + cancel routes, over
    # ``app.state.agent_task_registry`` (rebuilt at boot from agent-task sessions).
    register_agent_task_routes(app, deps)
    # ---- /v1/sessions/{sid}/async-processes (#1205) ----
    # Session-scoped union of spawned AgentTask rows and durable MCP TaskRecord
    # rows, kind-discriminated, for the tray's single fetch.
    register_async_process_routes(app, deps)

    # ---- /v1/artifacts + /v1/{sessions,workspaces}/{id}/artifacts (#966 S2/#968) ----
    artifact_workspace.register_artifact_workspace_routes(app, deps)

    # ---- /v1/workspaces -------------------------
    # Workspace store CRUD + file listing/reading are owned by
    # routes/workspaces.py; registered here so they bind to the same app.
    register_workspaces_routes(app, deps)

    # ---- /v1/agent-blueprints/* + /v1/expert-packs/* + session blueprint ---
    # Blueprint source registry, install/update/delete engine, MCP-descriptor
    # enable, and the session get/set-active-blueprint routes are owned by
    # routes/blueprints.py; the expert-pack routes are thin aliases of the same
    # lifecycle. The set-active route reaches the activation-metadata builder
    # and metadata-only active-id reader through ``deps``.
    register_blueprints_routes(app, deps)

    # ---- /v1/expert-packs/* discovery + session attachment -----------
    # Pack discovery (list/get/validate) and session attachment (get/set the
    # active pack) are owned by routes/expert_packs.py. (Pack install/update/delete
    # are blueprint-engine aliases registered above by register_blueprints_routes.)
    register_expert_packs_routes(app, deps)

    # ---- /v1/agents/* + /v1/sessions/{sid}/agent-overlay -------------
    # Tier-2 agent registry CRUD + list + extract and the session agent-overlay
    # routes (get/put/export) are owned by routes/agents.py; they reach the shared
    # row-resolution closures plus the destructive-action guard through ``deps``.
    register_agents_routes(app, deps)

    # ---- /v1/mcp/servers (#13) ---------------------------------------
    # MCP server registry + dispatch (list/detail/install/call/reconnect/
    # uninstall + tools/resources/prompts + handshake) are owned by
    # routes/mcp.py; the uninstall route reaches the destructive-action guard
    # through ``deps``.
    register_mcp_routes(app, deps)

    # ---- MCP Apps 2026-01-26 -----------------------------------------
    # Capability-bound app instance/resource/tool/message routes. Resource
    # reads and app-originated tool calls remain pinned to the exact MCP
    # namespace that produced the originating result.
    register_mcp_app_routes(app, deps)

    # ---- /v1/prompts (CLIO prompt-management vendor surface) ---------
    # Prompt registry browse/render/validate/save/reload are owned by
    # routes/prompts.py; the request-scoped registry/overlay/render-context
    # builders travel on ``deps``.
    register_prompts_routes(app, deps)

    # ---- /v1/sessions/{sid}/context/* (ARC live-context plane) -------
    # Session context compartment policy + the live ARC context-plane routes
    # (state/ops/compact/search) are owned by routes/context.py; the
    # state-assembly + ARC-unavailable helpers they share live there.
    register_context_routes(app, deps)
    register_trace_routes(app, deps)
    register_provenance_routes(app, deps)

    # ---- /v1/sessions/{sid}/diffs/* + /context/files + /context/frames ---
    # Pending/applied file-diff list/apply/reject plus the context-file
    # attach/detach/list ledger and per-turn context frames are owned by
    # routes/diffs.py; the diff-to-disk commit + ledger flush + destructive-
    # action guard travel on ``deps``.
    register_diffs_routes(app, deps)

    # ---- /v1/memory/* (transcript-memory recall surface) -------------
    # The read-only memory search + the three agent-callable, policy-gated
    # memory tools (search-sessions / read-session-summary / read-context-frame)
    # are owned by routes/memory.py. The ranked-search primitives they share with
    # the agent-run path live in runtime/memory_search.py (single source); the
    # error/audit/policy + bounded-projection helpers are private to that module.
    register_memory_routes(app, deps)

    # ---- /v1/sessions/{sid}/schedules + /v1/schedules/{id} (#21) -----
    # Scheduled-turn CRUD (list/add/delete) is owned by routes/schedules.py; the
    # delete route reaches the destructive-action guard through ``deps`` and the
    # scheduler tick task owns the actual firing of due schedules.
    register_schedules_routes(app, deps)

    # ---- /v1/health + /v1/capabilities + /v1/capability-gaps + /v1/metrics ----
    # + /v1/memory/stats: the read-only system/observability surface is owned by
    # routes/system.py. The static capability/metrics catalogs it projects live in
    # runtime/capabilities.py (shared with the message-turn streaming path here);
    # the wire/limit constants live in runtime/constants.py. It needs no
    # cross-concern seam from ``deps``.
    register_system_routes(app, deps)
    register_relay_routes(app, deps)

    # ---- /v1/sessions/{sid}/tasks + /v1/tasks/{tid} + memory/events + share ----
    # + /v1/shared/{token} + /v1/sessions/{sid}/events SSE: the misc session-
    # adjacent surfaces are owned by routes/misc.py; the task-delete route reaches
    # the direct-destructive-action guard through ``deps``.
    register_misc_routes(app, deps)

    # ---- /v1/catalog/tools + /v1/tools + /v1/commands + dispatch -----
    # The tool catalog (built-in + unified live) and the slash-command catalog +
    # dispatch are owned by routes/catalog.py. The command-table assembly lives in
    # runtime/commands.py (shared with the prompt-render-context closure here); the
    # dispatch route reaches the message-ledger primitives, agent runner, and
    # destructive-action guard through ``deps``.
    register_catalog_routes(app, deps)

    # ---- /v1/providers (#15) + /v1/providers/lm ---
    # The LM-provider catalog (list/detail/auth/models/handshake) and the runtime
    # LM-bind routes (get/put/wait LM config) are owned by routes/providers.py. The
    # write-side bind hot-swaps the live agent's LMs and mutates
    # ``dspy.settings.main_thread_config`` + ``os.environ`` (snapshot/restore on
    # failure); it reaches the agent-rebuild hooks (install-tool-runtime-hooks /
    # clear-session-model-refs) through ``deps``.
    register_providers_routes(app, deps)
    register_provider_models_refresh_routes(app, deps)  # POST .../models/refresh (#1211)

    # ---- /v1/catalog/tools + /v1/tools + /v1/tools/{tool_id} ----------
    # The built-in tool catalog and the unified live catalog (bundled gateway +
    # installed third-party MCP servers) are owned by routes/catalog.py and
    # registered below via register_catalog_routes(app, deps).

    # ---- /v1/permissions (BBB23) + /v1/policies (SPEC §6.11.b) --------
    # Permission-request ledger CRUD (list/resolve) + declarative permission-
    # policy CRUD (list/replace) are owned by routes/permissions.py; the
    # resolution-derived-policy + validation/persistence data layer lives in
    # runtime/permission_policies.py (shared with the build_app startup load).
    register_permissions_routes(app, deps)

    # ---- DELETE /v1/sessions/{sid}/messages/{id} + /v1/messages/{id} -
    # Both message-delete routes (session-scoped + the global, optionally
    # session-hinted variant gact-tui historically hit) are owned by
    # routes/messages.py and registered below via register_messages_routes(
    # app, deps); the destructive-action guard + ledger replace travel on
    # ``deps`` and both publish message.deleted for SSE subscribers.

    def _error_code_for_status(status_code: int) -> str:
        if status_code == 404:
            return "not_found"
        if status_code == 405:
            return "unsupported"
        if status_code in {400, 422}:
            return "validation_error"
        if status_code in {401, 403}:
            return "permission_error"
        return "internal_error" if status_code >= 500 else "request_error"

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request, exc: StarletteHTTPException) -> JSONResponse:
        """Wrap HTTPExceptions in the v0.2 error envelope."""

        if isinstance(exc.detail, dict) and "error" in exc.detail:
            # Already an envelope (caller built one explicitly).
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error=_error_code_for_status(exc.status_code),
                message=str(exc.detail) if exc.detail else "",
                recoverable=exc.status_code < 500,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(exclude_none=True),
        )

    @app.exception_handler(SkillNotDelegatableError)
    async def _skill_not_delegatable(request, exc: SkillNotDelegatableError) -> JSONResponse:
        """Typed 400 for a skill id used as an agent id (#918)."""
        info = ErrorInfo(
            error="skill_not_delegatable",
            message=str(exc),
            details={"skill_id": exc.skill_id, "skill_path": exc.path},
            recoverable=True,
        )
        return JSONResponse(
            status_code=400, content=ErrorEnvelope(error=info).model_dump(exclude_none=True)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request, exc: RequestValidationError) -> JSONResponse:
        """Wrap FastAPI request validation failures in the GACT envelope."""

        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="validation_error",
                message="Request validation failed.",
                details={"errors": jsonable_encoder(exc.errors())},
                recoverable=True,
            )
        )
        return JSONResponse(
            status_code=422,
            content=envelope.model_dump(exclude_none=True),
        )

    # The Exception backstop is registered by install_error_envelope above,
    # paired with the middleware it must agree with.

    # --- optional web UI (`clio web`): serve the built SPA bundle same-origin ---
    # Gated on CLIO_WEB_DIR so the default server (TUI / headless API) is byte-for-
    # byte unchanged unless web mode is explicitly enabled. Mounted LAST so every
    # /v1 API route (and /docs, /openapi.json) registered above takes precedence;
    # an SPA fallback serves index.html for unknown non-API paths so client-side
    # (history) routing works. The bundle's API calls are same-origin (relative
    # /v1/...), so no CORS/proxy is needed — this is the in-process equivalent of
    # the docker clio-web nginx setup.
    web_dir = _web_dir()
    if web_dir and (Path(web_dir) / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles
        from starlette.responses import FileResponse

        class _SPAStaticFiles(StaticFiles):
            async def get_response(self, path: str, scope: Any) -> Any:
                try:
                    return await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code == 404:
                        return FileResponse(Path(web_dir) / "index.html")
                    raise

        app.mount("/", _SPAStaticFiles(directory=web_dir, html=True), name="web")

    return app


# Module-level ``app`` for uvicorn-style invocations:
#   uvicorn clio_agent.gact.app:app
#
# Built lazily via PEP 562 module ``__getattr__`` so that ``import
# clio_agent.gact.app`` (which the ``clio-agent-gact`` console script
# triggers) doesn't pay build_app's cost — that includes pulling in
# clio_agent.tools.execution + litellm (~4 s on Aurora's frameworks
# Python). main() constructs its own app explicitly, so the only
# consumer of this attribute is the ``uvicorn …:app`` form, which
# always materialises it on first request anyway.
_lazy_app: Optional[FastAPI] = None


def __getattr__(name: str):
    global _lazy_app  # noqa: PLW0603
    if name == "app":
        if _lazy_app is None:
            _lazy_app = build_app()
        return _lazy_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run_server(
    host: str = "127.0.0.1",
    port: int = 8100,
    *,
    reload: bool = False,
    no_agent: bool = False,
) -> None:
    """Build the GACT app and run it in the foreground via uvicorn.

    This is the single foreground-serve path shared by the
    ``clio-agent-gact`` console script (:func:`main`) and the
    ``clio-agent serve`` subcommand. It blocks until the server exits.

    When ``CLIO_LM_PROVIDER`` is set (and ``no_agent`` is False) the real
    ``ClioAgent`` is constructed by the lifespan startup task so POST
    /messages drives a real LM; otherwise the app runs agent-less (fine for
    capability introspection, 503s on /messages).

    Args:
        host: Bind host.
        port: Bind port.
        reload: uvicorn auto-reload on source changes (dev only).
        no_agent: Skip ClioAgent construction even when LM env is configured.
    """
    import uvicorn

    # Resolve trace verbosity (file→env→default) and install the formatted log
    # handler for the server process, now that the environment is settled.
    trace.configure()

    # Always build a fresh app here — the module-level ``app`` symbol is
    # intentionally lazy (see __getattr__ above) so that just importing
    # ``clio_agent.gact.app`` doesn't pay build_app's cost. When the env
    # requests an agent we set want_agent so the lifespan startup task
    # constructs ClioAgent in the background — uvicorn binds the port
    # immediately, beating gact-tui's 3-second deploy probe. POST /messages
    # 503s until app.state.agent is stamped by the background task.
    app_to_run: FastAPI = build_app()
    if (
        not no_agent
        and conf.resolve("lm.provider", env="CLIO_LM_PROVIDER", default="", cast=conf.as_str) != ""
    ):
        app_to_run.state.want_agent = True

    uvicorn.run(
        app_to_run,
        host=host,
        port=port,
        reload=reload,
    )


def main() -> None:
    """Console-script entry point.

    When ``CLIO_LM_PROVIDER`` is set the real ``ClioAgent`` is
    instantiated + injected so POST /messages drives a real LM.
    Otherwise the module-level ``app`` (no agent wired) runs, which
    is fine for capability introspection but 503s on /messages.
    """

    parser = argparse.ArgumentParser(
        prog="clio-agent-gact",
        description="CLIO's GACT v0.2 REST + SSE server.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8100, type=int)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="auto-reload on source changes (dev only)",
    )
    parser.add_argument(
        "--no-agent",
        action="store_true",
        help=(
            "skip ClioAgent construction even when LM env is configured. "
            "Use when the real agent's boot cost (DSPy + ARC hydration) "
            "gets in the way of a capability-only smoke."
        ),
    )
    # gact-tui's `agent deploy` invokes adapters with --cwd; we don't
    # care about the value (CLIO reads file paths from CLIO_ALLOWED_ROOTS
    # / its own config), but the flag has to be accepted or argparse
    # bails with exit 2 and the deploy probe sees an instant zombie.
    parser.add_argument(
        "--cwd",
        default=None,
        help=(
            "ignored — accepted for compatibility with `gact agent "
            "deploy clio`, which always passes --cwd."
        ),
    )
    args = parser.parse_args()

    run_server(
        host=args.host,
        port=args.port,
        reload=args.reload,
        no_agent=args.no_agent,
    )


def main_deprecated() -> None:
    """Deprecation alias for the ``clio-agent-gact`` console script.

    ``clio-agent serve`` is now the single front door. This alias stays
    fully functional for one release so old installed launchers that still
    call ``clio-agent-gact`` keep working; it just emits a one-line stderr
    notice before delegating to :func:`main`.
    """

    print(
        "clio-agent-gact is deprecated; use clio-agent serve",
        file=sys.stderr,
    )
    main()
