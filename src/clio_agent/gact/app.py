"""GACT v0.2 FastAPI application for CLIO.

Exposes the GACT v0.2 contract surface. Most routes are 501 stubs
today (CLIO-BBBBBBBBBB6); they get wired one at a time in
follow-on iterations (BBB7–BBB12) against the spec at
``gact-tui/contract/SPEC.md`` and the docs in ``docs/tui/``.

Run via::

    clio-agent-gact --host 127.0.0.1 --port 8100

Or::

    uvicorn clio_agent.gact.app:app --host 127.0.0.1 --port 8100

This is a peer of ``clio_agent.ui.api`` (the native CLIO REST API),
not a replacement — both can run side-by-side. The TUI integration
target is the GACT app; existing CLI + direct-Python callers keep
using the native API unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from clio_agent import conf
from clio_agent.gact import context as _ctx
from clio_agent.gact.semantic_events import (
    DEFAULT_DETAIL_LEVEL,
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
    _EXPERT_CHILDREN_CACHE,
    _ORCHESTRATOR_BRIEFING_CACHE,
    _PROCESS_ARC,
    ARC_OP_EVENT_TYPE,
    _active_lm_last_reasoning,
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _BlueprintTerminalWorkflowState,
    _build_semantic_event,
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
    _jsonish,
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
    _with_ui_safe_semantic_fields,
)

_EXECUTABLE_SESSION_AGENT_IDS = {
    "",
    "main",
    "default",
}


def _gact_turn_timeout_s(app: Optional["FastAPI"] = None) -> float:
    """Return the per-turn no-progress timeout in seconds; <=0 disables it.

    Precedence: a RUNTIME value set via ``PUT /v1/providers/lm`` (``turn_timeout_s``,
    stored on ``app.state.lm_config``) wins, so a client configures this on the
    SAME channel it configures the LM — no disconnected server-launch env. When
    unset (0/absent), fall back to the conf pathway (file → ``CLIO_GACT_TURN_TIMEOUT_S``
    → 900s default).
    """
    if app is not None:
        cfg = getattr(getattr(app, "state", None), "lm_config", None)
        if isinstance(cfg, Mapping):
            try:
                runtime = conf.as_float(cfg.get("turn_timeout_s") or 0)
            except (ValueError, TypeError):
                runtime = 0.0
            if runtime > 0:
                return runtime
    try:
        return conf.resolve(
            "limits.turn_timeout_s",
            env="CLIO_GACT_TURN_TIMEOUT_S",
            default=900.0,
            cast=conf.as_float,
        )
    except (ValueError, TypeError):
        return 900.0


def _keyword_user_agent_routing_enabled() -> bool:
    """Return whether legacy keyword routing into user agents is enabled."""

    raw = os.environ.get("CLIO_ENABLE_KEYWORD_USER_AGENT_ROUTING", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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


# Session message-ledger + workspace-mirror + context-file helpers now live in
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
from clio_agent.gact.session_store import (  # noqa: E402,F401
    _HISTORY_MAX_CHARS_PER_MESSAGE,
    _HISTORY_MAX_MESSAGES,
    _append_session_message,
    _compile_session_conversation_history,
    _delete_session_context_files,
    _delete_session_messages,
    _extend_session_messages,
    _flush_context_files,
    _load_context_files,
    _mirror_workspace_messages,
    _mirror_workspace_session,
    _release_session_arc,
    _remove_workspace_session_mirror,
    _replace_session_messages,
    _workspace_for_session,
    _workspace_storage_root_for_session,
)


def _cancelled_error_info(
    sid: str,
    *,
    execution_cancellation: str,
    executor_work_may_continue: bool,
) -> "ErrorInfo":
    return ErrorInfo(
        error="cancelled",
        message="turn cancelled by client",
        details={
            "session_id": sid,
            "execution_cancellation": execution_cancellation,
            "executor_work_may_continue": executor_work_may_continue,
        },
        recoverable=True,
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
from clio_agent.gact.agents.composition import (  # noqa: E402, F401
    _agent_prompt_request,
    _agent_rows_prompt_render_context,
    _apply_prompt_registry_to_agent,
    _prompt_render_context,
    _prompt_resolution_metadata,
    _runtime_active_workspace_context,
    _runtime_dynamic_agent_children_context,
)
from clio_agent.gact.agents.resolution import (  # noqa: E402, F401
    _agent_definition_is_agent_blueprint,
    _agent_definition_uses_blueprint_runtime,
    _agent_overlay_patchable_fields,
    _legacy_native_expert_runtime_enabled,
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


def _keyword_routed_user_agent(app: "FastAPI", text: str) -> "AgentDef | None":
    """Return the best registered user agent whose keyword matches text.

    This intentionally ignores auto-discovered skills for now. Skills can be
    numerous and global, so implicit routing only uses agents the user
    registered directly in this CLIO backend.
    """

    normalized = f" {re.sub(r'[^a-z0-9_+-]+', ' ', text.lower())} "
    matches: list[tuple[int, str, AgentDef]] = []
    for row in app.state.user_agents.list():
        agent = AgentDef(**row.to_wire())
        for raw_keyword in agent.keywords:
            keyword = str(raw_keyword or "").strip().lower()
            if not keyword:
                continue
            needle = f" {re.sub(r'[^a-z0-9_+-]+', ' ', keyword)} "
            if needle.strip() and needle in normalized:
                matches.append((len(keyword), agent.id, agent))
                break
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]))
    return matches[0][2]


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
from clio_agent.gact._params import (  # noqa: E402,F401
    _user_agent_bool_param,
    _user_agent_float_param,
    _user_agent_int_param,
    _user_agent_param,
)

# gact/agents/builders.py + agents/runtime.py -- expert/blueprint runtime engine;
# the kept turn-handler dispatch wrappers below reach the builders through these.
from clio_agent.gact.agents.builders import (  # noqa: E402,F401
    _active_base_agent_tool_executor,
    _adapter_tool_intent_from_exception,
    _blueprint_fanout_config,
    _blueprint_runtime_signature,
    _build_blueprint_dspy_module,
    _build_child_expert_tool,
    _build_fanout_tool,
    _build_prompt_user_agent_module,
    _build_tool_user_agent_module,
    _call_enabled_external_mcp_tool,
    _call_recovered_dspy_tool,
    _coerce_fanout_child_ids,
    _dynamic_agent_lm_config,
    _dynamic_agent_tools,
    _dynamic_child_expert_tools,
    _emit_blueprint_llm_failure,
    _emit_invalid_tool_selection_event,
    _enabled_external_mcp_dspy_tools,
    _extract_repair_attempts,
    _invalid_tool_selection_from_exception,
    _is_repairable_typed_output_error,
    _prompt_user_agent_signature,
    _recording_blueprint_tool,
    _recover_blueprint_react_tool_intent,
    _reextract_over_retained_trajectory,
    _repair_temperature,
    _run_external_mcp_tool_sync,
    _tool_names,
    _tool_user_agent_max_iters,
    _tool_user_agent_signature,
    _typed_output_repair_hint,
)
from clio_agent.gact.agents.runtime import (  # noqa: E402,F401
    _prediction_structured_metadata,
    _retaining_react_cls,
    _summarize_segments_llm,
)

# gact/delegation.py -- delegation + workflow-state derivation cluster.
from clio_agent.gact.delegation import (  # noqa: E402,F401
    _append_accumulated_workflow_state_context,
    _append_nested_workflow_state,
    _append_session_workflow_state_context,
    _bubbled_child_evidence_output_summary,
    _coerce_expert_handoff_rows,
    _compact_dynamic_delegation_output,
    _compact_exact_evidence_index,
    _compact_workflow_state_blocks,
    _delegated_expert_agent_id,
    _delegated_expert_prompt,
    _dynamic_parent_resume_prompt,
    _expert_handoff_summary,
    _failed_child_delegation_output_summary,
    _failed_child_delegation_workflow_state,
    _iter_delegation_return_rows,
    _json_objects_from_text,
    _latest_completed_artifact_output_summary,
    _latest_completed_child_output_summary,
    _latest_delegation_output_summary,
    _latest_final_child_output_summary,
    _latest_parent_resumed_output_summary,
    _looks_like_truncated_user_facing_tail,
    _merge_workflow_state_from_value,
    _should_execute_delegated_handoff,
    _state_path_value,
    _state_predicate_hit,
    _strip_embedded_workflow_state_evidence,
    _user_facing_dynamic_evidence_summary,
    _workflow_state_from_handoff_rows,
    _workflow_state_from_outputs,
    _workflow_state_has_existing_staged_path,
    _workflow_state_payload,
)

# gact/evidence.py -- evidence-grounding + tool-result / trajectory-evidence.
from clio_agent.gact.evidence import (  # noqa: E402,F401
    _bounded_tool_call_result,
    _dynamic_agent_runtime_provenance,
    _extract_tools_called_from_trajectory,
    _ground_fabricated_local_artifact_paths,
    _is_bounded_tool_result,
    _is_empty_dynamic_agent_answer_error,
    _is_remote_artifact_ref,
    _propose_edit_diffs_from_pred,
    _tool_agent_empty_answer_fallback,
    _tool_result_is_error,
    _tool_result_preview,
    _verified_local_artifact_paths_by_ext,
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
    _normalize_workflow_state_section,
    _trajectory_key_index,
    _value_has_semantic_content,
    _workflow_status_rank,
)


def _append_prediction_workflow_state(output: str, result: Any) -> str:
    """Append a blueprint prediction's first-class workflow_state output."""

    raw_state = getattr(result, "workflow_state", None)
    if raw_state in (None, ""):
        return output
    if isinstance(raw_state, str):
        text = raw_state.strip()
        if not text:
            return output
        block = text
    else:
        # A typed workflow_state output field may arrive as a Pydantic model
        # (when a pack declares it as a nested object signature field) or as a
        # plain dict. Normalize any model to JSON-able structures so the nested
        # object survives serialization and downstream parsing instead of being
        # stringified into an unparseable repr. This is generic for all packs.
        normalized_state = _jsonish(raw_state)
        payload = (
            normalized_state
            if isinstance(normalized_state, Mapping) and "workflow_state" in normalized_state
            else {"workflow_state": normalized_state}
        )
        block = json.dumps(payload, sort_keys=True, default=str)
    if block in output:
        return output
    return f"{output.rstrip()}\n\nCLIO typed workflow state:\n{block}".strip()


def _fallback_answer_from_delegation(handoffs: list[dict[str, Any]]) -> str:
    """Return the latest compact parent-resume output as answer fallback."""

    for row in reversed(handoffs):
        if str(row.get("stage") or "") != "parent.resumed":
            continue
        if str(row.get("status") or "") not in {"", "completed"}:
            continue
        text = str(row.get("output_summary") or "").strip()
        if text:
            return text
    return ""


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
from clio_agent.gact.routes.agents import (  # noqa: E402
    register_agents_routes,
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
from clio_agent.gact.routes.hooks import (  # noqa: E402
    register_hooks_routes,
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
from clio_agent.gact.routes.providers import (  # noqa: E402
    register_providers_routes,
)
from clio_agent.gact.routes.schedules import (  # noqa: E402
    register_schedules_routes,
)
from clio_agent.gact.routes.sessions import (  # noqa: E402
    register_sessions_routes,
)
from clio_agent.gact.routes.system import (  # noqa: E402
    register_system_routes,
)
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
    BACKEND_COMMANDS as _BACKEND_COMMANDS,
)
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


def _tool_call_event_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a stable identity for de-duplicating tool telemetry events."""
    call_id = str(call.get("call_id") or "").strip()
    if call_id:
        return "__call_id__", call_id
    return _tool_call_name_args_key(call)


def _tool_call_name_args_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a tool-name/arguments identity for posthoc trajectory rows."""

    name = str(call.get("name") or call.get("tool") or "")
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    try:
        encoded_args = json.dumps(args or {}, sort_keys=True, default=str)
    except TypeError:
        encoded_args = str(args or {})
    return name, encoded_args


def _tool_call_has_result_evidence(call: Mapping[str, Any]) -> bool:
    """Return whether a tool-call row carries auditable result evidence."""

    for key in ("result", "observation", "output", "response", "result_preview"):
        value = call.get(key)
        if value in (None, "", [], {}):
            continue
        return True
    return False


def _normalize_tool_call_row(call: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a tool-call row while preserving bounded result evidence."""

    row: dict[str, Any] = {}
    call_id = str(call.get("call_id") or "").strip()
    if call_id:
        row["call_id"] = call_id
    name = call.get("name") or call.get("tool")
    if name:
        row["name"] = str(name)
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    if args is not None:
        row["args"] = args
    for key in ("ok", "duration_ms", "cached", "error", "telemetry_source"):
        if key in call:
            row[key] = call[key]
    for key in ("result", "observation", "output", "response", "result_preview"):
        if key not in call:
            continue
        value = call.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "result":
            row["result"] = _bounded_tool_call_result(value)
        else:
            row[key] = _bounded_tool_call_result(value)
        break
    if row and "telemetry_source" not in row:
        row["telemetry_source"] = "posthoc_prediction"
    return row


def _merge_tool_call_rows(
    primary_rows: list[dict[str, Any]],
    supplemental_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge tool-call telemetry without dropping richer result evidence."""

    merged: list[dict[str, Any]] = [_normalize_tool_call_row(row) for row in primary_rows if row]
    by_key: dict[tuple[str, str], list[int]] = {}
    by_name_args: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(merged):
        by_key.setdefault(_tool_call_event_key(row), []).append(index)
        by_name_args.setdefault(_tool_call_name_args_key(row), []).append(index)

    for raw_supplemental in supplemental_rows:
        supplemental = _normalize_tool_call_row(raw_supplemental)
        if not supplemental:
            continue
        key = _tool_call_event_key(supplemental)
        candidate_index: int | None = None
        supplemental_has_result = _tool_call_has_result_evidence(supplemental)
        supplemental_ok = supplemental.get("ok")
        candidate_indexes = list(by_key.get(key, []))
        if not candidate_indexes and (
            not supplemental.get("call_id") or not supplemental_has_result
        ):
            fallback_indexes = by_name_args.get(_tool_call_name_args_key(supplemental), [])
            if supplemental_has_result:
                fallback_indexes = [
                    index for index in fallback_indexes if merged[index].get("ok") is not False
                ]
            if len(fallback_indexes) == 1:
                candidate_indexes = fallback_indexes
        for index in candidate_indexes:
            existing = merged[index]
            existing_ok = existing.get("ok")
            if key[0] == "__call_id__":
                candidate_index = index
                break
            if supplemental_has_result and existing_ok is False and supplemental_ok is not False:
                continue
            if supplemental_has_result and not _tool_call_has_result_evidence(existing):
                candidate_index = index
                break
            if not supplemental_has_result:
                candidate_index = index
                break
        if candidate_index is None:
            by_key.setdefault(key, []).append(len(merged))
            by_name_args.setdefault(_tool_call_name_args_key(supplemental), []).append(len(merged))
            merged.append(supplemental)
            continue

        existing = merged[candidate_index]
        for field_name, value in supplemental.items():
            if field_name in {"result", "observation", "output", "response", "result_preview"}:
                if not _tool_call_has_result_evidence(existing):
                    existing[field_name] = value
                continue
            if value in (None, "", [], {}):
                continue
            if field_name not in existing or existing[field_name] in (None, "", [], {}):
                existing[field_name] = value
            elif field_name in {"duration_ms", "cached", "telemetry_source", "ok", "error"}:
                existing[field_name] = value
    return merged


def _tool_calls_from_handoff_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return nested child tool-call evidence from delegation rows."""

    tool_rows: list[dict[str, Any]] = []

    def visit(row: Any) -> None:
        if not isinstance(row, Mapping):
            return
        for call in row.get("tools_called") or []:
            if isinstance(call, Mapping):
                tool_rows.append(_normalize_tool_call_row(call))
        for child in row.get("children") or []:
            visit(child)

    for row in rows:
        visit(row)
    return tool_rows


def _clear_session_model_refs(app: "FastAPI") -> None:
    """Clear per-session model refs after a global LM provider swap.

    CLIO executes every turn through the active global LM. Existing
    sessions may still carry stale GACT ModelRefs from older TUI
    versions or emulator-compatible defaults; leaving those refs in
    place makes the next send fail with a per-session override error
    even though the user just changed the global provider correctly.
    """

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return
    for sess in sessions.list():
        if not _model_ref_is_empty(sess.model):
            sessions.update(sess.id, model={})


# --------------------------------------------------------------------------- #
# Turn-orchestration engine extracted to gact/turn.py (#714 decomposition).      #
#                                                                               #
# ``_run_turn_in_background`` (the off-thread turn loop, with its nested         #
# ``_settle_dynamic_agent_delegations`` / ``_execute_delegated_experts``         #
# delegation settlers) and ``_start_background_user_turn`` (the staging          #
# entrypoint) were carved out verbatim into ``clio_agent.gact.turn`` so the      #
# route factories + the scheduler tick can share the entrypoint without          #
# importing back into this module. They are re-exported here so existing         #
# ``from clio_agent.gact.app import <name>`` callers + the import-seam guardrail  #
# stay green; ``_start_background_user_turn`` is the explicit-``app`` engine the  #
# thin ``build_app`` closure wrapper (and ``GactDeps``) delegate to.             #
# --------------------------------------------------------------------------- #
from clio_agent.gact.turn import (  # noqa: E402,F401
    _run_turn_in_background,
    _start_background_user_turn,
)

# Alias kept so the thin ``build_app`` closure wrapper (which shadows the
# ``_start_background_user_turn`` name locally) can still reach the explicit-``app``
# engine; the unaliased name above is the module-level re-export the import-seam
# guardrail + ``from clio_agent.gact.app import _start_background_user_turn`` use.
_turn_start_background_user_turn = _start_background_user_turn


def _current_lm_model_id() -> str:
    """Best-effort: which model is dspy.settings.lm bound to."""
    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return ""
    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    return getattr(lm, "model", "") if lm else ""


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
from clio_agent.gact.permission_gate import (  # noqa: E402,F401
    _DESTRUCTIVE_TOOL_SUBSTRINGS,
    _SAFE_READONLY_UTILS,
    _SAFE_RESHAPE_UTILS,
    _UNSAFE_SHELL_TOKENS,
    _direct_permission_denied,
    _guard_direct_destructive_action,
    _is_destructive,
    _is_safe_readonly_diagnostic,
    _is_safe_shell_diagnostic,
    _is_safe_text_reshape_command,
    _make_cancellation_checker,
    _make_permission_gate,
    _policy_action_for_tool,
    _record_resolved_permission,
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
    _usage_from_history_slice_legacy,
    _usage_from_tracker,
)


def _install_tool_runtime_hooks(app: "FastAPI") -> None:
    """Install permission, cancellation, and telemetry hooks for tool calls."""

    from clio_agent.tools.execution import (  # noqa: PLC0415
        set_global_cancellation_checker,
        set_global_permission_gate,
        set_global_tool_interceptor,
        set_global_tool_observer,
    )

    checker = getattr(app.state, "pending_cancellation_checker", None)
    if checker is None:
        checker = _make_cancellation_checker(app)
    gate = getattr(app.state, "pending_permission_gate", None)
    if gate is None:
        gate = _make_permission_gate(app)
    observer = getattr(app.state, "pending_tool_observer", None)
    if observer is None:
        observer = _make_tool_observer(app)
    interceptor = getattr(app.state, "pending_tool_interceptor", None)
    set_global_cancellation_checker(checker)
    set_global_permission_gate(gate)
    set_global_tool_interceptor(interceptor)
    set_global_tool_observer(observer)
    app.state.pending_cancellation_checker = checker
    app.state.pending_permission_gate = gate
    app.state.pending_tool_interceptor = interceptor
    app.state.pending_tool_observer = observer
    app.state.tool_hooks_installed = True


def _ensure_live_assistant_message(app: "FastAPI", sid: str) -> str:
    """Return the in-flight assistant message id, creating it if needed."""

    live_ids = getattr(app.state, "live_assistant_message_ids", None)
    if live_ids is None:
        live_ids = {}
        app.state.live_assistant_message_ids = live_ids
    msg_id = str(live_ids.get(sid) or "")
    if msg_id:
        return msg_id
    msg_id = _new_message_id("asst")
    live_ids[sid] = msg_id
    now = _iso_from_epoch(time.time())
    app.state.bus.publish(
        Event(
            type="message.created",
            session_id=sid,
            payload=Message(
                id=msg_id,
                turn_id=_active_semantic_turn_id(),
                session_id=sid,
                role="assistant",
                created_at=now,
                updated_at=now,
                parts=[],
            ).model_dump(exclude_none=True),
        )
    )
    return msg_id


def _append_live_assistant_part(app: "FastAPI", sid: str, part: Part) -> None:
    """Publish and remember a real runtime part for the active assistant turn."""

    msg_id = _ensure_live_assistant_message(app, sid)
    live_parts = getattr(app.state, "live_assistant_parts", None)
    if live_parts is None:
        live_parts = {}
        app.state.live_assistant_parts = live_parts
    live_parts.setdefault(sid, []).append(part)
    app.state.bus.publish(
        Event(
            type="message.part.added",
            session_id=sid,
            payload={
                "turn_id": _active_semantic_turn_id(),
                "message_id": msg_id,
                # Real runtime parts (tool calls/results, routing) emitted live during
                # the turn (#711); not provider-token text, but emitted in real time.
                "stream_source": str(part.metadata.get("stream_source") or "live"),
                "part": part.model_dump(exclude_none=True),
            },
        )
    )


def _append_live_assistant_part_once(
    app: "FastAPI",
    sid: str,
    key: str,
    part: Part,
) -> bool:
    """Publish a live part once per in-flight turn.

    Tool observers can fire many times for the same routed expert. The
    transcript should show the route decision once, then the concrete tool
    calls/results under it, not repeat the same route banner for every call.
    """

    live_keys = getattr(app.state, "live_assistant_part_keys", None)
    if live_keys is None:
        live_keys = {}
        app.state.live_assistant_part_keys = live_keys
    session_keys = live_keys.setdefault(sid, set())
    if key in session_keys:
        return False
    session_keys.add(key)
    _append_live_assistant_part(app, sid, part)
    return True


def _agent_tool_owner(app: "FastAPI", tool_name: str) -> tuple[str, str]:
    """Return (public_parent, owner) for a tool if CLIO can resolve it."""

    agent = getattr(app.state, "agent", None)
    if agent is None:
        return "", ""
    candidates = [tool_name]
    if "." in tool_name:
        candidates.append(tool_name.rsplit(".", 1)[-1])
    for candidate in candidates:
        try:
            owner = str(agent._selected_expert_for_tool(candidate) or "")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            continue
        if not owner:
            continue
        try:
            parent = str(agent._parent_route_for_child(owner) or "")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            parent = ""
        return parent or owner, owner
    return "", ""


def _emit_live_tool_route_context(app: "FastAPI", sid: str, tool_name: str) -> None:
    """Emit route/handoff context immediately before a live tool call."""

    public_agent, owner = _agent_tool_owner(app, tool_name)
    if not public_agent or public_agent in {"chat", "none"}:
        return
    _append_live_assistant_part_once(
        app,
        sid,
        f"route:{public_agent}",
        Part(
            id=f"live_route_{public_agent}",
            type="routing_decision",
            selected_agent=public_agent,
            rationale=f"Agent planner selected {public_agent} for tool {tool_name}.",
            confidence=0.0,
            heuristic=False,
            metadata={
                "route_source": "live_tool_observer",
                "route_reason": f"Resolved from live tool owner {owner}.",
                "stream_source": "live",
            },
            execution_path=f"orchestrator -> {public_agent}",
        ),
    )
    if owner and owner != public_agent:
        row = {
            "agent_id": owner,
            "parent_id": public_agent,
            "dispatch_target": owner,
            "status": "running",
            "stage": "tool.started",
            "delegation_lifecycle": "sync",
            "execution_mode": "tool",
            "depth": 1,
            "output_summary": f"Preparing {tool_name}.",
        }
        _append_live_assistant_part_once(
            app,
            sid,
            f"handoff:{public_agent}:{owner}",
            Part(
                id=f"live_handoff_{public_agent}_{owner}",
                type="expert_handoff",
                text=_expert_handoff_summary(row),
                metadata={**row, "stream_source": "live", "route_source": "live_tool_observer"},
            ),
        )


def _make_tool_observer(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.tool_observer.

    Publishes tool.call.started / tool.call.completed events into
    the EventBus, attaching to the active turn session when present
    and falling back to recency only for out-of-band calls. Also
    appends each completed call into ``app.state.tool_call_ledger[sid]`` so the
    turn handler can attach a per-turn ``tools_called`` list to the
    assistant message metadata even when the underlying expert
    didn't populate ``pred.tools_called`` itself (e.g. the
    deterministic short-circuit paths).
    """

    def observe(
        name: str,
        args: Mapping[str, Any],
        phase: Optional[str],
        error: Optional[str],
        result: Any | None = None,
    ) -> None:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return
        if phase == "started":
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            # Stash the per-thread call_id so the completion event
            # uses the same id. Threading-locals works for
            # MCPToolBridge's worker thread.
            _OBSERVER_CALL_IDS.value = call_id
            # Stamp the start time so completion can compute duration.
            _OBSERVER_CALL_T0.value = time.time()
            _emit_live_tool_route_context(app, sid, name)
            _emit_semantic_event(
                app,
                sid,
                "tool.call.started",
                turn_id=_ctx.active_turn_id(),
                trace_id=_ctx.active_trace_id(),
                status="running",
                summary=f"Tool {name} started.",
                actor={"tool": name},
                subject={"call_id": call_id},
                payload={
                    "call_id": call_id,
                    "tool": name,
                    "args": dict(args),
                    "telemetry_source": "live_observer",
                },
            )
            app.state.bus.publish(
                Event(
                    type="tool.call.started",
                    session_id=sid,
                    payload={
                        "call_id": call_id,
                        "tool": name,
                        "args": dict(args),
                        "telemetry_source": "live_observer",
                    },
                )
            )
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_{call_id}_call",
                    type="tool_call",
                    call_id=call_id,
                    tool_name=name,
                    input=dict(args),
                    metadata={"stream_source": "live", "telemetry_source": "live_observer"},
                ),
            )
        elif phase == "completed":
            call_id = getattr(_OBSERVER_CALL_IDS, "value", "") or ""
            t0 = getattr(_OBSERVER_CALL_T0, "value", None)
            duration_ms = (time.time() - t0) * 1000 if t0 else 0.0
            cancel_event = app.state.cancel_events.get(sid)
            completed_after_cancel = sid in app.state.cancel_flags or (
                cancel_event is not None and cancel_event.is_set()
            )
            completion_error = error
            cancellation_metadata: dict[str, Any] = {}
            if completed_after_cancel:
                completion_error = (
                    completion_error or "tool call completed after session cancellation"
                )
                cancellation_metadata = {
                    "execution_cancellation": "best_effort",
                    "executor_work_may_continue": True,
                }
            ok = completion_error is None
            result_summary = f"Tool {name} {'completed' if ok else 'failed'}."
            payload = {
                "call_id": call_id,
                "tool": name,
                "ok": ok,
                "duration_ms": duration_ms,
                "cached": False,
                "telemetry_source": "live_observer",
                "ui_summary": result_summary,
                "result_summary": result_summary,
                **({"error": completion_error} if completion_error else {}),
                **({"result": _bounded_tool_call_result(result)} if result is not None else {}),
                **cancellation_metadata,
            }
            # Append to the per-session ledger FIRST -- before the (potentially
            # I/O-bound, e.g. durable-trace-writing) semantic emit + live parts --
            # so the turn handler's post-forward drain never races a slow emit and
            # drops tools_called from the assistant message metadata.
            ledger = getattr(app.state, "tool_call_ledger", None)
            if ledger is not None and not completed_after_cancel:
                ledger.setdefault(sid, []).append(
                    {
                        "name": name,
                        "call_id": call_id,
                        "args": dict(args),
                        "ok": ok,
                        "duration_ms": duration_ms,
                        "cached": False,
                        "telemetry_source": "live_observer",
                        **({"error": completion_error} if completion_error else {}),
                        **(
                            {"result": _bounded_tool_call_result(result)}
                            if result is not None
                            else {}
                        ),
                        **cancellation_metadata,
                    }
                )
            # Canonical trace captures the FULL tool result (never capped) -- the
            # bounded projection in `payload` is only for the wire bus event +
            # ledger/assistant-metadata. (SSE still redacts `result` via
            # SENSITIVE_KEYS; only the durable trace keeps the full value.)
            trace_payload = {**payload, "result": result} if result is not None else payload
            _emit_semantic_event(
                app,
                sid,
                "tool.call.completed",
                turn_id=_ctx.active_turn_id(),
                trace_id=_ctx.active_trace_id(),
                status="completed" if ok else "failed",
                summary=result_summary,
                actor={"tool": name},
                subject={"call_id": call_id},
                payload=trace_payload,
            )
            app.state.bus.publish(
                Event(
                    type="tool.call.completed",
                    session_id=sid,
                    payload=payload,
                )
            )
            result_text = completion_error or (
                _tool_result_preview(result) if result is not None else "completed"
            )
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_{call_id}_result",
                    type="tool_result",
                    call_id=call_id,
                    tool_name=name,
                    is_error=not ok,
                    duration_ms=duration_ms,
                    cached=False,
                    content=[
                        Part(
                            id=f"live_{call_id}_result_text",
                            type="text",
                            text=result_text,
                        )
                    ],
                    metadata={
                        "stream_source": "live",
                        "telemetry_source": "live_observer",
                        **(
                            {"result": _bounded_tool_call_result(result)}
                            if result is not None
                            else {}
                        ),
                        **cancellation_metadata,
                    },
                ),
            )

    return observe


_OBSERVER_CALL_T0 = threading.local()


def _agent_forward_compat(
    agent: Any,
    question: str,
    session_id: str,
    session_mode: str,
    session_edit_mode: str,
    cancel_requested: Any | None = None,
    images: list[Any] | None = None,
) -> Any:
    """Call agent.forward, threading session_mode + session_edit_mode
    when the agent accepts them, falling back to the legacy
    ``(question, session_id)`` signature for fakes / older builds.

    Lets us add new optional kwargs to the contract without breaking
    every test fixture that hand-rolled a minimal forward signature.
    """

    optional_kwargs: dict[str, Any] = {
        "images": images or [],
        "cancel_requested": cancel_requested,
    }
    attempts = [
        optional_kwargs,
        {"cancel_requested": cancel_requested},
        {"images": images or []},
        {},
    ]
    last_type_error: TypeError | None = None
    for optional in attempts:
        try:
            return agent.forward(
                question,
                session_id=session_id,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
                **optional,
            )
        except TypeError as exc:
            message = str(exc)
            if "images" not in message and "cancel_requested" not in message:
                last_type_error = exc
                break
            last_type_error = exc

    try:
        return agent.forward(question, session_id=session_id)
    except TypeError as exc:
        if last_type_error is not None:
            raise last_type_error from exc
        raise


async def _try_streamed_forward_compat(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk: Any,
    *,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
    images: list[Any] | None = None,
    agent_override: Any | None = None,
    cancel_requested: Any | None = None,
) -> Optional[Any]:
    """Call _try_streamed_forward with a legacy-signature fallback for tests/plugins."""

    base_kwargs: dict[str, Any] = {
        "session_mode": session_mode,
        "session_edit_mode": session_edit_mode,
    }
    if agent_override is not None:
        base_kwargs["agent_override"] = agent_override

    optional_attempts: list[dict[str, Any]] = [
        {"images": images or [], "cancel_requested": cancel_requested},
        {"cancel_requested": cancel_requested},
        {"images": images or []},
        {},
    ]
    last_type_error: TypeError | None = None
    for optional in optional_attempts:
        try:
            return await _try_streamed_forward(
                app,
                enriched_text,
                sid,
                emit_chunk,
                **base_kwargs,
                **optional,
            )
        except TypeError as exc:
            message = str(exc)
            if "cancel_requested" not in message and "images" not in message:
                raise
            last_type_error = exc
    if last_type_error is not None:
        raise last_type_error
    return None


def _run_dynamic_agent_compat(
    runner: Any,
    base_agent: Any,
    dynamic_agent: Any,
    question: str,
    sid: str,
    cancel_requested: Any | None,
) -> Any:
    """Run a dynamic agent while preserving older runner call signatures."""

    try:
        return runner(base_agent, dynamic_agent, question, sid, cancel_requested)
    except TypeError as exc:
        if "positional" not in str(exc) and "argument" not in str(exc):
            raise
        return runner(base_agent, dynamic_agent, question, sid)


_OBSERVER_CALL_IDS = threading.local()


class _StreamingOutputError(RuntimeError):
    """Raised when live streaming fails after user-visible output was emitted."""


def _stream_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build structured metadata for a batch text delivery path."""

    definition = _STREAM_FALLBACK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown stream fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: list(value) if isinstance(value, list) else value
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def _stream_fallback_reasons(app: "FastAPI") -> dict[str, dict[str, Any]]:
    reasons = getattr(app.state, "stream_fallback_reasons", None)
    if not isinstance(reasons, dict):
        reasons = {}
        app.state.stream_fallback_reasons = reasons
    return reasons


def _record_stream_fallback(
    app: "FastAPI",
    sid: str,
    reason: str,
    message: str = "",
) -> None:
    _stream_fallback_reasons(app)[sid] = _stream_fallback_payload(reason, message)


def _pop_stream_fallback(app: "FastAPI", sid: str) -> dict[str, Any]:
    return _stream_fallback_reasons(app).pop(sid, {})


def _append_stream_listener(
    listeners: list[Any],
    stream_listener_cls: Any,
    *,
    signature_field_name: str,
    predict: Any,
) -> None:
    if predict is None:
        return
    try:
        listeners.append(
            stream_listener_cls(
                signature_field_name=signature_field_name,
                predict=predict,
            )
        )
    except Exception:  # noqa: BLE001
        return


def _build_stream_listeners(agent: Any, stream_listener_cls: Any) -> list[Any]:
    """Build explicit DSPy stream listeners for CLIO's known predictors.

    Auto-discovering by field name is fragile here because several CLIO
    predictors expose the same output fields. Explicit predictor binding
    lets chat, final synthesis, and expert outputs stream live without
    fighting over repeated names like ``answer`` or ``analysis``.
    """

    listeners: list[Any] = []
    _append_stream_listener(
        listeners,
        stream_listener_cls,
        signature_field_name="answer",
        predict=getattr(agent, "chat_agent", None),
    )
    _append_stream_listener(
        listeners,
        stream_listener_cls,
        signature_field_name="answer",
        predict=getattr(agent, "answer_synthesizer", None),
    )

    return listeners


def _agent_streaming_unsupported_reason(agent: Any) -> str:
    """Return a fallback reason when the active provider cannot stream live.

    Only the CLI-backed custom transports (``codex`` JSON-RPC, ``claude_code``
    exec) are genuinely non-streaming. Argonne/ALCF (Sophia + Metis) is a plain
    OpenAI-compatible SSE endpoint: it streams at the provider AND through LiteLLM
    (verified: multi-chunk incremental deltas), so it must NOT be force-classified
    as batch. Hardcoding it here bypassed the streamify pump for EVERY ALCF run
    (iowarp/clio-agent#160). The streamify path below has its own graceful
    try/except fallback to sync, so letting argonne attempt streaming can only
    improve on the previous always-batch behaviour.
    """

    provider_config = getattr(agent, "_provider_config", None)
    provider = str(getattr(provider_config, "provider", "") or "")
    provider_kind = _provider_runtime_kind(provider)
    if provider_kind in {"claude_code", "codex"}:
        return "provider_streaming_unsupported"
    # iowarp/clio-agent#639: normalize the preset id (argonne_sophia/_metis) to
    # the provider kind (argonne) BEFORE the capability check. Reasoning models on
    # the ALCF gateways stream their answer on the reasoning_content channel,
    # which DSPy's content-only stream listeners can't fold and which fails the
    # streamify task group ("live streaming failed before emitting output"). Route
    # them through the robust blocking path (which recovers reasoning_content via
    # _process_completion). Scoped to argonne reasoning models: non-reasoning ALCF
    # (gpt-oss/gemma) still streams (#160), and lm_studio reasoning models (qwopus)
    # stream content fine, so they are untouched.
    if provider_kind == "argonne" and _config_is_reasoning_model(provider_config):
        return "provider_streaming_unsupported"
    return ""


def _config_is_reasoning_model(provider_config: Any) -> bool:
    """Whether a provider config is a reasoning model (handshake ``is_reasoning``
    / per-model capability). Used to keep reasoning models off streaming paths
    that lose the reasoning_content channel."""

    if provider_config is None:
        return False
    try:
        from clio_agent.config import _reasoning_model_capability  # noqa: PLC0415

        return bool(_reasoning_model_capability(provider_config))
    except Exception:
        return bool(getattr(provider_config, "is_reasoning", False))


def _stream_response_prefix(field_name: str, previous_field_name: str) -> str:
    """Return formatting to insert when a streamed output field starts."""

    if not field_name or field_name == previous_field_name:
        return ""
    if field_name == "recommendations":
        return "\n\nRecommendations:\n"
    if field_name == "file_path":
        return "\n\nFile: "
    return ""


# Minimum gap between reasoning-channel heartbeats. The watchdog only needs
# *a* progress event within its window (default 900s), so a 1s throttle keeps a
# deep-reasoning turn alive without flooding the bus with one event per token.
_REASONING_HEARTBEAT_S = 1.0


def _describe_stream_exc(exc: BaseException) -> str:
    """Format a streaming exception for logging, UNWRAPPING ``ExceptionGroup``.

    ``streamify`` runs the agent forward inside an anyio task group, so a failure
    surfaces as ``ExceptionGroup`` whose ``str()`` is only the opaque wrapper
    ("unhandled errors in a TaskGroup (1 sub-exception)") — the real cause lives
    in ``.exceptions``. Recurse into the leaves so the captured detail names the
    actual provider/transport error instead of the wrapper.
    """
    group = getattr(exc, "exceptions", None)
    if group:
        leaves = "; ".join(_describe_stream_exc(sub) for sub in group)
        return f"{type(exc).__name__}[{leaves}]"
    return f"{type(exc).__name__}: {exc}"


async def _try_streamed_forward(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
    agent_override: Any | None = None,
    cancel_requested: Any | None = None,
) -> Optional[Any]:
    """Run the agent's forward via dspy.streamify, pumping every
    text chunk through ``emit_chunk(text)`` as it arrives. Returns
    the final dspy.Prediction on success, or None if streaming is
    unavailable before invoking the agent. Streaming execution failures
    raise ``_StreamingOutputError`` so the caller can surface the failed
    turn instead of rerunning it as batch fallback text.

    Falls back before output when the agent isn't a DSPy module, when
    streamify import fails, or when the wrapped call doesn't yield
    parsable text chunks. The fallback synchronous path produces
    the same wire shape (just no live deltas).
    """

    # Guided/structured output streams as reasoning_content-only deltas on
    # LM Studio (no content deltas), which the assembly below can't fold into
    # content -> empty content -> parse failure. Return None so the caller falls
    # back to the blocking path, whose content<-reasoning_content fallback
    # (_process_completion) recovers the constrained JSON. TODO: fold reasoning
    # deltas into the stream assembly to re-enable live streaming under guided output.
    try:
        from clio_agent.config import _guided_output_enabled  # noqa: PLC0415

        if _guided_output_enabled():
            _record_stream_fallback(app, sid, "stream_disabled_guided_output")
            return None
    except Exception:  # noqa: BLE001 - never let this gate break the turn
        pass

    # Some reasoning-model + provider combos stream the answer entirely on the
    # reasoning_content delta channel (which content-only stream listeners miss
    # and which bypasses _process_completion's content<-reasoning_content
    # recovery) or fail the streamify task group outright. Routing them through
    # the blocking path recovers the answer. Default ON (unchanged for every
    # model that streams cleanly); opt out per model via CLIO_LIVE_STREAMING=0.
    try:
        from clio_agent.config import _live_streaming_enabled  # noqa: PLC0415

        if not _live_streaming_enabled():
            _record_stream_fallback(app, sid, "stream_disabled_live_streaming")
            return None
    except Exception:  # noqa: BLE001 - never let this gate break the turn
        pass

    try:
        import dspy  # noqa: PLC0415
        from dspy.streaming.messages import StreamResponse  # noqa: PLC0415
        from dspy.streaming.streamify import streamify
        from dspy.streaming.streaming_listener import StreamListener  # noqa: PLC0415
        from litellm.types.utils import ModelResponseStream  # noqa: F401
    except Exception as exc:
        _record_stream_fallback(
            app,
            sid,
            "streaming_dependency_unavailable",
            f"{type(exc).__name__}: {exc}",
        )
        return None

    agent = agent_override if agent_override is not None else app.state.agent
    if agent is None:
        _record_stream_fallback(app, sid, "agent_not_available")
        return None
    if not isinstance(agent, dspy.Module):
        _record_stream_fallback(app, sid, "agent_not_streamable")
        return None
    unsupported_reason = _agent_streaming_unsupported_reason(agent)
    if unsupported_reason:
        _record_stream_fallback(app, sid, unsupported_reason)
        return None

    # iowarp/clio-agent#158: bind listeners to explicit Predict instances
    # instead of asking DSPy to infer them by output field name.
    listeners = _build_stream_listeners(agent, StreamListener)
    # is_async_program=True is only valid for modules with a real async
    # forward implementation. dspy.Module exposes acall generically, but
    # its default implementation delegates to aforward; ClioAgent only has
    # sync forward today, so treating inherited acall as sufficient forces
    # streamify into AttributeError and silently drops to synthetic fallback.
    has_async_forward = callable(getattr(agent, "aforward", None))
    try:
        streamed = streamify(
            agent,
            async_streaming=True,
            stream_listeners=listeners,
            is_async_program=has_async_forward,
        )
    except Exception as exc:
        # Stream binding is best-effort. If DSPy cannot attach the
        # listener to this program shape, let the canonical sync path
        # run and surface any real agent/provider error from there.
        _record_stream_fallback(
            app,
            sid,
            "stream_setup_failed",
            f"{type(exc).__name__}: {exc}",
        )
        return None

    final_pred = None
    emitted_any = False
    previous_stream_field = ""
    # Seed the reasoning-heartbeat clock so the first reasoning chunk publishes
    # immediately (refreshing the watchdog the moment the model starts thinking).
    last_reasoning_heartbeat = time.monotonic() - _REASONING_HEARTBEAT_S

    async def _emit_visible_chunk(text: str, field_name: str = "") -> None:
        nonlocal emitted_any, previous_stream_field
        prefix = _stream_response_prefix(field_name, previous_stream_field)
        if prefix:
            await emit_chunk(prefix)
            emitted_any = True
        await emit_chunk(text)
        emitted_any = True
        if field_name:
            previous_stream_field = field_name

    try:
        # StreamListener emits ``StreamResponse`` instances that
        # carry the cleaned chunk in ``.chunk``. Keep the legacy
        # ``ModelResponseStream`` / dict / str fallback for backends
        # that don't surface a typed listener payload.
        # Pass session_mode + session_edit_mode if the agent's
        # forward signature accepts them (newer ClioAgent does;
        # older / fake agents fall back via TypeError catch).
        try:
            stream_iter = streamed(
                question=enriched_text,
                session_id=sid,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
                cancel_requested=cancel_requested,
            )
        except TypeError:
            try:
                stream_iter = streamed(
                    question=enriched_text,
                    session_id=sid,
                    session_mode=session_mode,
                    session_edit_mode=session_edit_mode,
                )
            except TypeError:
                stream_iter = streamed(question=enriched_text, session_id=sid)
        async for piece in stream_iter:
            if isinstance(piece, dspy.Prediction):
                final_pred = piece
                continue
            if isinstance(piece, StreamResponse):
                if piece.chunk:
                    await _emit_visible_chunk(
                        piece.chunk, getattr(piece, "signature_field_name", "") or ""
                    )
                continue
            text_chunk = _chunk_text(piece)
            if text_chunk:
                await _emit_visible_chunk(text_chunk)
                continue
            # No answer-content in this chunk -- but the model may be actively
            # streaming REASONING tokens (a separate delta channel invisible to
            # DSPy's content-only listeners). Publishing a throttled, session-
            # scoped heartbeat refreshes the no-progress watchdog so a deep-
            # reasoning expert call isn't killed mid-think. We DON'T route the
            # reasoning into the answer part (it would pollute the answer); the
            # event carries it under a distinct type a TUI may render as
            # "thinking", and -- crucially -- advances bus.last_publish_monotonic.
            reasoning_chunk = _chunk_reasoning_text(piece)
            if reasoning_chunk:
                now = time.monotonic()
                if now - last_reasoning_heartbeat >= _REASONING_HEARTBEAT_S:
                    last_reasoning_heartbeat = now
                    try:
                        app.state.bus.publish(
                            Event(
                                type="agent.reasoning.delta",
                                session_id=sid,
                                payload={"stream_source": "reasoning"},
                            )
                        )
                    except Exception:  # noqa: BLE001 - heartbeat is best-effort
                        pass
    except Exception as exc:
        detail = _describe_stream_exc(exc)
        if emitted_any:
            raise _StreamingOutputError(
                f"live streaming failed after emitting output: {detail}"
            ) from exc
        _record_stream_fallback(
            app,
            sid,
            "stream_failed_before_output",
            detail,
        )
        raise _StreamingOutputError(
            f"live streaming failed before emitting output: {detail}"
        ) from exc
    if emitted_any and final_pred is None:
        raise _StreamingOutputError(
            "live streaming ended after emitting output without a final prediction"
        )
    if final_pred is None:
        _record_stream_fallback(app, sid, "stream_no_prediction")
    elif not emitted_any:
        _record_stream_fallback(
            app,
            sid,
            "stream_completed_without_chunks",
            "DSPy streamify returned a final prediction but emitted no visible text chunks.",
        )
    return final_pred


def _chunk_reasoning_text(piece: Any) -> str:
    """Pull reasoning-channel text out of a streamify chunk.

    Reasoning models (qwopus, nemotron, …) stream their chain-of-thought on a
    SEPARATE delta channel (``delta.reasoning_content`` / ``delta.reasoning``),
    not ``delta.content``. DSPy's StreamListener only watches ``delta.content``
    for ``[[ ## field ## ]]`` markers, so reasoning tokens are invisible to it.
    For an unlistened predict (every blueprint expert), streamify yields the raw
    chunk straight through to our pump -- but ``_chunk_text`` returns "" for it
    (content is empty during thinking). We extract the reasoning channel here so
    the pump can refresh the no-progress watchdog while the model is *actively
    thinking* (a deep-reasoning expert call can stream tens of thousands of
    reasoning tokens with zero answer-content tokens; treating that as "no
    progress" wrongly kills a working model -- see the EarthScope resolver hang).
    """

    if not piece or isinstance(piece, (str, dict)):
        # dict shape handled below in the rare OpenAI-dict path; str is answer text.
        if isinstance(piece, dict):
            try:
                delta = piece["choices"][0]["delta"]
                return str(delta.get("reasoning_content") or delta.get("reasoning") or "")
            except (KeyError, IndexError, TypeError):
                return ""
        return ""
    try:
        choices = piece.choices  # type: ignore[attr-defined]
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta is not None:
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    return str(reasoning)
    except Exception:  # noqa: BLE001 - best-effort extraction
        pass
    return ""


def _chunk_text(piece: Any) -> str:
    """Pull a string out of whatever streamify yielded.

    Handles litellm ModelResponseStream + plain str + dict shapes.
    Returns "" when nothing's there (status-message-only chunks
    don't pollute the part body).
    """

    if isinstance(piece, str):
        return piece
    # litellm stream chunks: choices[0].delta.content
    try:
        choices = piece.choices  # type: ignore[attr-defined]
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if content:
                    return str(content)
    except Exception:
        pass
    if isinstance(piece, dict):
        # OpenAI-style dict.
        try:
            return piece["choices"][0]["delta"].get("content", "") or ""
        except (KeyError, IndexError, TypeError):
            return ""
    return ""


def _format_react_trajectory(traj: Any) -> str:
    """Render a DSPy ReAct trajectory (a list/dict of steps) as a
    human-readable trace. Returns "" when the input doesn't look
    like a trajectory.
    """

    if not traj:
        return ""
    rows: list[str] = []
    if isinstance(traj, dict):
        # ReAct stores as {step_n_thought, step_n_action, ...}
        idx = 0
        while True:
            thought = traj.get(f"step_{idx}_thought") or traj.get(f"thought_{idx}")
            action = traj.get(f"step_{idx}_tool_name") or traj.get(f"action_{idx}")
            if thought is None and action is None:
                break
            row = []
            if thought:
                row.append(f"thought: {thought}")
            if action:
                row.append(f"action: {action}")
            rows.append("  ".join(row))
            idx += 1
    elif isinstance(traj, list):
        for i, step in enumerate(traj):
            if isinstance(step, dict):
                rows.append(f"step {i}: {step}")
            else:
                rows.append(f"step {i}: {step!r}")
    return "\n".join(rows)


def _extract_tools_called(pred: Any) -> list[dict[str, Any]]:
    """Pull an agent prediction's tool-call trace into a wire-shaped
    list.

    The tier-2 experts expose their tool calls on
    ``pred.tools_called`` when the ReAct loop tracks them. Each
    entry is either a ``clio_agent.arc.schema.ToolCall`` (msgspec
    struct), a plain dict, or an object with attribute access —
    handle all three. Fields copied onto the wire when present:
    name, args, ok, duration_ms, cached. All optional.
    """

    raw = getattr(pred, "tools_called", None)
    if not raw:
        return []

    out: list[dict[str, Any]] = []
    for call in raw:
        row: dict[str, Any] = {}
        agent_trace_call = False
        if isinstance(call, dict):

            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return _src.get(key, default)
        else:
            # msgspec structs + DSPy trace records — attribute access.
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return getattr(_src, key, default)

            agent_trace_call = (
                hasattr(call, "tool") and hasattr(call, "params") and hasattr(call, "result")
            )

        name = get("name") or get("tool") or ""
        if name:
            row["name"] = str(name)

        args = get("args")
        if args is None:
            args = get("arguments")
        if args is None:
            args = get("params")
        if args is not None:
            row["args"] = args

        status = get("status")
        if status is not None:
            row["ok"] = status not in {"failure", "error", "timeout"}
        elif get("ok") is not None:
            row["ok"] = bool(get("ok"))

        duration_ms = get("duration_ms")
        if duration_ms is not None:
            row["duration_ms"] = float(duration_ms)

        cached = get("cached")
        if cached is not None:
            row["cached"] = bool(cached)

        result = get("result")
        if result is not None:
            row["result"] = _bounded_tool_call_result(result)
            if "ok" not in row and agent_trace_call:
                row["ok"] = not (
                    (isinstance(result, dict) and "error" in result)
                    or (isinstance(result, str) and result.startswith("Error:"))
                )

        telemetry_source = get("telemetry_source") or (
            "agent_trace" if agent_trace_call else "posthoc_prediction"
        )
        row["telemetry_source"] = str(telemetry_source)

        if row:
            out.append(row)
    return out


def _signature_prompt(signature: Any) -> str:
    """Return a cleaned DSPy signature docstring for catalog display."""
    return inspect.cleandoc(getattr(signature, "__doc__", "") or "")


# --- re-export shim (#714): skills/commands/catalog loading moved to catalog.py ---
from typing import Protocol

from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    load_agent_blueprint_path,
    load_agent_blueprints,
    load_mcp_descriptors,
    read_install_metadata,
    validate_agent_hierarchy,
)
from clio_agent.gact.catalog import (  # noqa: E402, F401
    _builtin_agents,
    _builtin_tools,
    _command_search_roots,
    _default_skill_id,
    _fallback_skill_keywords,
    _load_command_files_from_disk,
    _load_skills_from_disk,
    _normalize_file_command_id,
    _parse_skill_frontmatter,
    _skill_list_field,
    _skill_markdown_files,
    _skill_search_roots,
    _tool_owner_for_catalog,
    _tool_tags_for_catalog,
    _tool_visible_to_for_catalog,
    _truthy_command_field,
)
from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.expert_packs import (
    discover_expert_packs,
    load_expert_pack_path,
    load_expert_packs,
    validate_expert_hierarchy,
)
from clio_agent.gact.messages import MessageStore
from clio_agent.gact.sessions import SessionStore, _default_store_path
from clio_agent.gact.types import (
    AgentCapabilityRef,
    AgentDef,
    ErrorEnvelope,
    ErrorInfo,
    Message,
    Part,
    Session,
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
    task: Optional[asyncio.Task] = None
    if getattr(app.state, "schedules", None) is not None:
        task = asyncio.create_task(_scheduler_tick(app))
        app.state.scheduler_task = task

    agent_task: Optional[asyncio.Task] = None
    if getattr(app.state, "want_agent", False) and app.state.agent is None:
        agent_task = asyncio.create_task(_construct_agent_async(app))
        app.state.agent_construction_task = agent_task

    yield

    lm_config_task = getattr(app.state, "lm_config_task", None)
    for t in (task, agent_task, lm_config_task):
        if t is None:
            continue
        if getattr(t, "done", lambda: False)():
            continue
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _release_owned_lm_studio_instance(app, raise_on_error=False),
        )
    except Exception:
        pass
    # Drain + stop the off-loop semantic-trace writer so no events are lost on shutdown.
    _trace_backend = getattr(app.state, "semantic_trace_backend", None)
    _trace_close = getattr(_trace_backend, "close", None)
    if callable(_trace_close):
        try:
            _trace_close()
        except Exception:  # pragma: no cover - defensive shutdown cleanup
            pass
    if getattr(app.state, "tool_hooks_installed", False):
        try:
            from clio_agent.tools.execution import (  # noqa: PLC0415
                set_global_cancellation_checker,
                set_global_permission_gate,
                set_global_tool_interceptor,
                set_global_tool_observer,
            )

            set_global_cancellation_checker(None)
            set_global_permission_gate(None)
            set_global_tool_interceptor(None)
            set_global_tool_observer(None)
        except Exception:  # pragma: no cover - defensive shutdown cleanup
            pass


async def _construct_agent_async(app: "FastAPI") -> None:
    """Build the real ClioAgent off the lifespan hot path.

    DSPy import + ARC hydration + expert wiring takes ~10 s on Aurora's
    frameworks Python (beartype import hook + Lustre cold reads). We
    run it via ``run_in_executor`` so the event loop stays free for
    /v1/capabilities, /v1/health, and the rest of the catalog while
    the agent constructs. On success, stamps ``app.state.agent`` +
    ``app.state.arc`` so the next POST /messages dispatches normally;
    on failure, logs and leaves ``agent=None`` so /messages keeps
    surfacing a structured 503 instead of a corrupted half-built
    agent.
    """

    loop = asyncio.get_running_loop()
    # Construct (or reuse) the ONE per-process ARC up front and inject it into the build,
    # so the agent does not mint a fresh ARC — the same instance is app.state.arc for the
    # whole process across every later LM bind (no per-build ARC churn / trace ⊋ ARC split).
    arc = _process_arc(app)

    def _build() -> Any:
        import dspy  # noqa: PLC0415

        from clio_agent.agent import ClioAgent  # noqa: PLC0415
        from clio_agent.config import (  # noqa: PLC0415
            create_chat_adapter,
            create_lm,
            load_config_from_env,
        )

        cfg = load_config_from_env()
        dspy.configure(
            lm=create_lm(cfg),
            adapter=create_chat_adapter(cfg),
        )
        return ClioAgent(verbose=False, arc=arc)

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

    app.state.agent = agent
    # The agent's ARCMemory is built HERE (async), after build_app ran with arc=None;
    # _set_app_arc (re)wires the arc.op op-logger so ARC writes are observable.
    _set_app_arc(app, agent.arc)

    # Install the deferred permission gate + tool observer now that we
    # know an agent exists to gate. See build_app for why these aren't
    # installed at construction time.
    try:
        _install_tool_runtime_hooks(app)
    except Exception:  # pragma: no cover - defensive
        pass

    print("[clio-agent-gact] agent ready.", flush=True)


async def _scheduler_tick(app: "FastAPI") -> None:
    """Once-a-minute loop: fire any due schedules.

    Each due schedule kicks the same _run_turn_in_background path
    a regular POST /messages would, so SSE subscribers see the
    automated turn unfold like any other.
    """

    while True:
        try:
            now = datetime.now(timezone.utc)
            for sch in list(app.state.schedules.due_now(now)):
                scheduled_user_msg_id = _new_message_id("user")
                user_msg = Message(
                    id=scheduled_user_msg_id,
                    # A scheduled turn correlates to its own user message id (#711).
                    turn_id=scheduled_user_msg_id,
                    session_id=sch.session_id,
                    role="user",
                    created_at=_iso_from_epoch(time.time()),
                    updated_at=_iso_from_epoch(time.time()),
                    parts=[
                        Part(
                            id=_new_part_id(),
                            type="text",
                            text=sch.question,
                        )
                    ],
                    metadata={"scheduled": True, "schedule_id": sch.id},
                )
                _append_session_message(app, sch.session_id, user_msg)
                app.state.bus.publish(
                    Event(
                        type="message.created",
                        session_id=sch.session_id,
                        payload=user_msg.model_dump(exclude_none=True),
                    )
                )
                app.state.schedules.mark_fired(sch.id)
                # Fire-and-forget the turn task.
                asyncio.create_task(
                    _run_turn_in_background(
                        app,
                        sch.session_id,
                        sch.question,
                        user_msg,
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        # Sleep until just past the next minute boundary so we don't
        # double-fire on the same minute.
        await asyncio.sleep(60)


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

    # CORS: browser/WebView frontends must opt in with explicit origins.
    # CLIO's default auth scheme is trust_socket, which is safe for local
    # non-browser clients but must not grant arbitrary browser origins access
    # to a localhost agent. Operators can enable trusted web origins with
    # CLIO_GACT_CORS_ORIGINS (comma-separated origins or "*").
    cors_origins_env = os.environ.get("CLIO_GACT_CORS_ORIGINS", "").strip()
    if cors_origins_env:
        allow_origins: list[str] = (
            ["*"]
            if cors_origins_env == "*"
            else [o.strip() for o in cors_origins_env.split(",") if o.strip()]
        )
    else:
        allow_origins = []
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
    # CLIO-BBBBBBBBBB13: per-session pub/sub. POST /messages
    # publishes; /v1/sessions/{sid}/events subscribers consume.
    app.state.bus = EventBus()
    app.state.semantic_trace_detail_level = (
        os.environ.get(
            "CLIO_SEMANTIC_TRACE_DETAIL",
            DEFAULT_DETAIL_LEVEL,
        ).strip()
        or DEFAULT_DETAIL_LEVEL
    )
    app.state.semantic_trace_backend = build_trace_backend(
        session_store_path.parent / "semantic_traces"
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
        detail_level=app.state.semantic_trace_detail_level,
        live_consumers=None,
    )
    # (ARC's arc.op op-logger AND highway-derive sink are wired via _set_app_arc
    # whenever app.state.arc is assigned — see _set_app_arc; the highway closure reads
    # app.state.semantic_event_sink at fire-time, so this construction order is fine.)
    # CLIO-BBBBBBBBBB14: message log keyed by session_id. Populated by
    # POST /messages, read by GET /messages, and backed by per-session
    # JSON ledgers so adapter deletion/redeploy preserves transcripts.
    app.state.message_store = MessageStore(path=session_store_path.parent / "messages")
    app.state.messages = app.state.message_store.load_all()
    # CLIO-BBBBBBBBBB20: cooperative cancellation flags. POST /cancel
    # adds a sid; the POST-message handler checks + clears after the
    # agent returns. Set (not dict) because the flag's presence IS
    # the signal — no payload.
    app.state.cancel_flags = set()
    app.state.cancel_events = {}
    app.state.cancel_attempts = {}
    # CLIO-BBBBBBBBBB22: per-session context files. Keyed by
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
    # CLIO-BBBBBBBBBB21: per-session pending diffs. Keyed by
    # session_id -> list of {path, unified_diff, status,
    # part_id, message_id}. Status is "pending" until apply/reject
    # flips it.
    app.state.pending_diffs = {}
    # CLIO-BBBBBBBBBB23: pending permission requests. Flat dict
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
    # SPEC §6.17 hooks (declarative event→command/url callouts that
    # gact-tui drives via /v1/hooks). Distinct from CLIO's runtime
    # in-process Python hooks (clio_agent.runtime.hooks) — these are
    # user-configurable callouts the agent fires during the turn
    # lifecycle, while the Python runtime hooks are framework-level
    # extension points. In-memory; not persisted across restarts.
    app.state.declarative_hooks = {}
    # SPEC §6.11.b permission policies — list, not dict. Backends
    # consult this on every tool call to decide allow/deny/ask before
    # falling back to the per-tool permission_default. PUT replaces
    # the whole list.
    app.state.permission_policies_path = session_store_path.parent / "permission_policies.json"
    app.state.permission_policies = _load_permission_policies(app.state.permission_policies_path)
    # iowarp/clio-agent#18: per-session task list (todo-style).
    # Keyed by session_id -> {task_id -> task dict}. In-memory.
    app.state.session_tasks = {}
    # iowarp/clio-agent#3: per-session in-flight turn tasks. POST
    # /messages tracks the asyncio.Task here so /cancel can
    # hard-abort instead of waiting for the cooperative flag check.
    app.state.in_flight_turns = {}
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
    if agent is not None:
        try:
            _install_tool_runtime_hooks(app)
        except Exception:  # pragma: no cover - defensive
            pass
    else:
        app.state.tool_hooks_installed = False
        app.state.pending_cancellation_checker = _make_cancellation_checker(app)
        app.state.pending_permission_gate = _make_permission_gate(app)
        app.state.pending_tool_observer = _make_tool_observer(app)

    # iowarp/clio-agent#20: install the user-hooks registry so
    # pre_tool / post_tool / pre_message / post_message events
    # route to ~/.config/clio-agent/hooks/<event>.py. Tests pre-
    # install their own registry; we only install a default if
    # nothing's currently wired so the test-side hook stays.
    try:
        from clio_agent.runtime.hooks import (
            _registry as _current_registry,
        )
        from clio_agent.runtime.hooks import (
            build_hook_registry,
            install_global_registry,
        )

        if _current_registry is None:
            registry = build_hook_registry()
            install_global_registry(registry)
            app.state.runtime_hook_registry_metadata = (
                registry.metadata() if hasattr(registry, "metadata") else {}
            )
        else:
            app.state.runtime_hook_registry_metadata = (
                _current_registry.metadata() if hasattr(_current_registry, "metadata") else {}
            )
    except Exception:  # pragma: no cover - defensive
        app.state.runtime_hook_registry_metadata = {
            "backend": "unavailable",
            "enabled": False,
            "error": "failed_to_initialize",
        }
        pass

    # CLIO-BBBBBBBBBB-D: live LM config — what the TUI configured
    # us with. Distinct from boot-time env because PUT /providers/lm
    # rebuilds the agent + DSPy config in-place.
    app.state.lm_config = None
    app.state.lm_config_status = {"state": "idle"}
    app.state.lm_config_task = None
    app.state.lm_studio_owned_instance = None
    # CLIO-BBBBBBBBBB-WS: workspaces store. Persisted alongside
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
            except Exception:
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
                            _resolve_runtime_dynamic_agent,
                            agent_id=agent_id,
                            cwd=_command_cwd_for_request(app, session_id),
                        )
                    ]
                    context["commands.agent_invocable"] = (
                        "\n".join(commands) or "(no agent-invocable commands)"
                    )
                except Exception:
                    pass
        return context

    # ---- /v1/sessions CRUD + delete -----------------------------------
    # Session create/list/get/patch + permission-gated delete are owned by
    # routes/sessions.py and registered below via register_sessions_routes(
    # app, deps); the workspace-session mirror + the delete cascade
    # (messages/context-files/ARC release) travel on ``deps``.

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

    # ---- /v1/providers (#15) + /v1/providers/lm (CLIO-BBBBBBBBBB-D) ---
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

    def _agent_with_capability_refs(agent_def: AgentDef) -> AgentDef:
        """Attach normalized capability metadata to an AgentDef row."""

        refs: list[AgentCapabilityRef] = [
            AgentCapabilityRef(kind="tool", id=tool_id, title=tool_id, source="builtin")
            for tool_id in agent_def.tools
        ]
        refs.extend(
            AgentCapabilityRef(kind="skill", id=skill_id, title=skill_id, source=agent_def.source)
            for skill_id in agent_def.skills
        )
        refs.extend(
            AgentCapabilityRef(
                kind="command",
                id=command_id,
                title=command_id,
                source="builtin",
            )
            for command_id in agent_def.commands
        )
        refs.extend(agent_def.capability_refs)

        if agent_def.id == "main":
            command_ids = set(agent_def.commands)
            for row in _BACKEND_COMMANDS:
                command_id = row["id"]
                if command_id in command_ids:
                    continue
                raw_status = row.get("status")
                status: Literal["available", "unavailable", "unknown"] = (
                    raw_status
                    if raw_status in {"available", "unavailable", "unknown"}
                    else "available"
                )
                refs.append(
                    AgentCapabilityRef(
                        kind="command",
                        id=command_id,
                        title=row.get("title", command_id),
                        description=row.get("description", ""),
                        source=row.get("source", "builtin"),
                        status=status,
                        metadata=({"error": row["error"]} if row.get("error") else {}),
                    )
                )
                command_ids.add(command_id)
            agent_def = agent_def.model_copy(update={"commands": sorted(command_ids)})

        if agent_def.source == "skill" and agent_def.id not in agent_def.skills:
            refs.append(
                AgentCapabilityRef(
                    kind="skill",
                    id=agent_def.id,
                    title=agent_def.title,
                    description=agent_def.description,
                    source=str(agent_def.metadata.get("skill_source", "skill")),
                    metadata={
                        "skill_path": agent_def.metadata.get("skill_path", ""),
                        "skill_layout": agent_def.metadata.get("skill_layout", ""),
                    },
                )
            )
            agent_def = agent_def.model_copy(update={"skills": [*agent_def.skills, agent_def.id]})

        deduped: list[AgentCapabilityRef] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.kind, ref.id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ref)

        return agent_def.model_copy(update={"capability_refs": deduped})

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

    def _enabled_agent_blueprint_mcp_tool_names(blueprint_id: str = "") -> set[str]:
        names: set[str] = set()
        for server in (getattr(app.state, "external_mcp_servers", {}) or {}).values():
            if not isinstance(server, Mapping):
                continue
            if str(server.get("status") or "") != "ready":
                continue
            if blueprint_id and str(server.get("agent_blueprint_id") or "") != blueprint_id:
                continue
            for tool in server.get("tools") or []:
                if not isinstance(tool, Mapping):
                    continue
                if not bool(tool.get("enabled")) or str(tool.get("status") or "") != "ready":
                    continue
                tool_name = str(tool.get("name") or tool.get("id") or "").strip()
                if tool_name:
                    names.add(tool_name)
        return names

    def _agent_blueprint_descriptor_tools(rows: list[AgentDef]) -> dict[str, str]:
        descriptors_by_tool: dict[str, str] = {}
        roots: dict[str, tuple[str, str]] = {}
        for row in rows:
            root_file = str(row.metadata.get("agent_blueprint_definition_path") or "").strip()
            if not root_file:
                continue
            roots[root_file] = (
                str(row.metadata.get("agent_blueprint_scope") or "session"),
                str(row.metadata.get("agent_blueprint_id") or ""),
            )
        for root_file, (scope, blueprint_id) in sorted(roots.items()):
            root = Path(root_file).expanduser().parent
            try:
                descriptors = load_mcp_descriptors(
                    root,
                    scope=scope,
                    blueprint_id=blueprint_id,
                )
            except Exception:
                continue
            for descriptor in descriptors:
                descriptor_id = str(descriptor.get("id") or "")
                for tool in descriptor.get("tools") or []:
                    if not isinstance(tool, Mapping):
                        continue
                    tool_name = str(tool.get("name") or tool.get("id") or "").strip()
                    if tool_name:
                        descriptors_by_tool[tool_name] = descriptor_id
        return descriptors_by_tool

    def _apply_agent_blueprint_mcp_descriptor_validation(rows: list[AgentDef]) -> list[AgentDef]:
        descriptor_tools = _agent_blueprint_descriptor_tools(rows)
        if not descriptor_tools:
            return rows
        out: list[AgentDef] = []
        for row in rows:
            enabled_tools = _enabled_agent_blueprint_mcp_tool_names(
                str(row.metadata.get("agent_blueprint_id") or "").strip()
            )
            errors = list(row.validation_errors)
            diagnostics = list(row.metadata.get("tool_diagnostics", []))
            for tool_name in row.tools:
                if tool_name not in descriptor_tools or tool_name in enabled_tools:
                    continue
                descriptor_id = descriptor_tools[tool_name]
                message = f"MCP tool requires explicit enablement: {tool_name}" + (
                    f" (descriptor: {descriptor_id})" if descriptor_id else ""
                )
                if message not in errors:
                    errors.append(message)
                if not any(
                    isinstance(diag, Mapping)
                    and str(diag.get("tool") or "") == tool_name
                    and str(diag.get("source") or "") == "agent_blueprint_mcp_descriptor"
                    for diag in diagnostics
                ):
                    diagnostics.append(
                        {
                            "tool": tool_name,
                            "status": "disabled",
                            "source": "agent_blueprint_mcp_descriptor",
                            "descriptor_id": descriptor_id,
                        }
                    )
            metadata = dict(row.metadata)
            if diagnostics:
                metadata["tool_diagnostics"] = diagnostics
            if errors != list(row.validation_errors):
                metadata["mcp_descriptor_validation_disabled"] = True
            out.append(
                row.model_copy(
                    update={
                        "enabled": row.enabled and not errors,
                        "validation_errors": errors,
                        "metadata": metadata,
                    }
                )
            )
        return out

    def _apply_enabled_agent_blueprint_mcp_tools(rows: list[AgentDef]) -> list[AgentDef]:
        out: list[AgentDef] = []
        cache: dict[str, set[str]] = {}
        for row in rows:
            blueprint_id = str(row.metadata.get("agent_blueprint_id") or "").strip()
            enabled_tools = cache.setdefault(
                blueprint_id,
                _enabled_agent_blueprint_mcp_tool_names(blueprint_id),
            )
            if not enabled_tools:
                out.append(row)
                continue
            row_tools = {str(tool).strip() for tool in row.tools if str(tool).strip()}
            resolved_tools = row_tools & enabled_tools
            if not resolved_tools:
                out.append(row)
                continue
            errors = [
                error
                for error in row.validation_errors
                if not any(
                    error.startswith(f"MCP tool requires explicit enablement: {tool}")
                    for tool in resolved_tools
                )
            ]
            diagnostics = [
                diag
                for diag in row.metadata.get("tool_diagnostics", [])
                if not (
                    isinstance(diag, Mapping)
                    and str(diag.get("source") or "") == "agent_blueprint_mcp_descriptor"
                    and str(diag.get("tool") or "") in resolved_tools
                )
            ]
            metadata = dict(row.metadata)
            if diagnostics:
                metadata["tool_diagnostics"] = diagnostics
            else:
                metadata.pop("tool_diagnostics", None)
            disabled_by_mcp_validation = bool(
                metadata.pop("mcp_descriptor_validation_disabled", False)
            )
            out.append(
                row.model_copy(
                    update={
                        "enabled": row.enabled or (disabled_by_mcp_validation and not errors),
                        "validation_errors": errors,
                        "metadata": metadata,
                    }
                )
            )
        return out

    def _active_session_agent_blueprint_rows(
        session_id: str = "",
        workspace_id: str = "",
    ) -> list[AgentDef]:
        if not session_id:
            return []
        rows = _base_session_agent_blueprint_rows(session_id=session_id, workspace_id=workspace_id)
        if rows:
            rows = _apply_session_agent_overlay(rows, session_id=session_id)
            prompt_registry = _prompt_registry_for_request(
                session_id=session_id,
                workspace_id=workspace_id,
            )
            rows = validate_agent_hierarchy(_merge_agent_def_rows(rows))
            rows = _apply_agent_blueprint_mcp_descriptor_validation(rows)
            rows = _apply_enabled_agent_blueprint_mcp_tools(rows)
            active_blueprint_id = _active_session_agent_blueprint_id(session_id)
            render_context = _prompt_render_context(app)
            render_context.update(_agent_rows_prompt_render_context(rows))
            render_context["session.active_agent_blueprint"] = (
                active_blueprint_id or "(no active agent blueprint)"
            )
            render_context["session.active_pack"] = active_blueprint_id or "(no active expert pack)"
            return [
                _apply_prompt_registry_to_agent(
                    app,
                    _agent_with_capability_refs(row),
                    prompt_registry=prompt_registry,
                    render_context=render_context,
                )
                for row in rows
            ]
        return []

    def _active_session_agent_blueprint_agent_ids(session_id: str = "") -> set[str]:
        return {
            row.id
            for row in _active_session_agent_blueprint_rows(session_id=session_id)
            if row.enabled
        }

    def _active_session_agent_blueprint_root_id(session_id: str = "") -> str:
        rows = _active_session_agent_blueprint_rows(session_id=session_id)
        if not rows:
            return ""
        requested_root = str(rows[0].metadata.get("agent_blueprint_root_expert") or "").strip()
        if requested_root and any(row.id == requested_root and row.enabled for row in rows):
            return requested_root
        roots = [row for row in rows if row.enabled and not row.parent_id]
        if len(roots) == 1:
            return roots[0].id
        enabled = [row for row in rows if row.enabled]
        if not enabled:
            return ""
        return sorted(enabled, key=lambda row: (row.tier, row.id))[0].id

    # Deliberately shadows the module-level ``agents.resolution`` re-export with a
    # ``build_app``-closure variant that binds ``app`` + the local
    # ``_active_session_agent_blueprint_rows``; the re-export above stays for
    # ``from clio_agent.gact.app import _resolve_runtime_dynamic_agent`` callers +
    # the import-seam guardrail. (Surfaced as F811 only after the turn engine -- the
    # other module-level consumer -- moved to gact/turn.py; #714.)
    def _resolve_runtime_dynamic_agent(  # noqa: F811
        agent_id: str,
        *,
        session_id: str = "",
        workspace_id: str = "",
        prompt_registry: PromptRegistry | None = None,
    ) -> "AgentDef | None":
        if session_id:
            for row in _active_session_agent_blueprint_rows(
                session_id=session_id,
                workspace_id=workspace_id,
            ):
                if row.id == agent_id and row.enabled:
                    return row
        return _resolve_dynamic_agent(app, agent_id, prompt_registry=prompt_registry)

    def _agent_rows(session_id: str = "", workspace_id: str = "") -> list[AgentDef]:
        cwd = _workspace_catalog_cwd(workspace_id=workspace_id, session_id=session_id)
        rows = _active_session_agent_blueprint_rows(
            session_id=session_id,
            workspace_id=workspace_id,
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
            + _load_skills_from_disk()
            + load_expert_packs(cwd=cwd, pack_id=active_pack_id)
            + explicit_session_rows
        )
        prompt_registry = _prompt_registry_for_request(
            session_id=session_id,
            workspace_id=workspace_id,
        )
        return [
            _apply_prompt_registry_to_agent(
                app,
                _agent_with_capability_refs(row),
                prompt_registry=prompt_registry,
            )
            for row in validate_expert_hierarchy(_merge_agent_def_rows(rows))
        ]

    # ---- /v1/agent-blueprints/* + /v1/expert-packs/* lifecycle + session
    # blueprint activation (iowarp/clio-agent#663) -----------------------
    # Blueprint source registry, install/update/delete engine, MCP-descriptor
    # enable, and the session-scoped get/set-active-blueprint routes are owned
    # by routes/blueprints.py and registered below via
    # ``register_blueprints_routes(app, deps)`` once ``deps`` is built. The
    # expert-pack routes are thin aliases of the blueprint lifecycle (one engine,
    # ``kind``-distinguished). The set-active route reaches the activation-metadata
    # builder + workspace-session mirror (and the metadata-only active-id reader)
    # through ``deps``.

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
    # destructive-action guard and workspace-session mirror through ``deps``.

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
        mirror_workspace_session=_mirror_workspace_session,
        agent_rows=_agent_rows,
        agent_with_capability_refs=_agent_with_capability_refs,
        base_session_agent_blueprint_rows=_base_session_agent_blueprint_rows,
        apply_agent_overlay_rows=_apply_agent_overlay_rows,
        append_session_message=_append_session_message,
        delete_session_messages=_delete_session_messages,
        blueprint_runner_for_agent=_blueprint_runner_for_agent,
        resolve_runtime_dynamic_agent=_resolve_runtime_dynamic_agent,
        start_background_user_turn=_start_background_user_turn,
        remove_workspace_session_mirror=_remove_workspace_session_mirror,
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
    # replace, workspace mirror + delete cascade, model-ref errors, evidence
    # index and resume text travel on ``deps``.
    register_sessions_routes(app, deps)

    # ---- /v1/sessions/{sid}/messages + /v1/messages (BBB9/BBB10/BBB27) ---
    # The session message ledger -- the turn-entry POST, the list/get reads,
    # substring search and both message-delete routes -- is owned by
    # routes/messages.py. The turn-entry POST kicks a background turn through
    # ``deps.start_background_user_turn``; the destructive-action guard, ledger
    # replace, active-model ref + override error and the agent-not-available
    # error travel on ``deps``.
    register_messages_routes(app, deps)

    # ---- /v1/workspaces (CLIO-BBBBBBBBBB-WS) -------------------------
    # Workspace store CRUD + file listing/reading are owned by
    # routes/workspaces.py; registered here so they bind to the same app.
    register_workspaces_routes(app, deps)

    # ---- /v1/agent-blueprints/* + /v1/expert-packs/* + session blueprint ---
    # Blueprint source registry, install/update/delete engine, MCP-descriptor
    # enable, and the session get/set-active-blueprint routes are owned by
    # routes/blueprints.py; the expert-pack routes are thin aliases of the same
    # lifecycle. The set-active route reaches the activation-metadata builder,
    # workspace-session mirror, and metadata-only active-id reader through
    # ``deps``.
    register_blueprints_routes(app, deps)

    # ---- /v1/expert-packs/* discovery + session attachment -----------
    # Pack discovery (list/get/validate) and session attachment (get/set the
    # active pack) are owned by routes/expert_packs.py; the set route reaches
    # the workspace-session mirror through ``deps``. (Pack install/update/delete
    # are blueprint-engine aliases registered above by register_blueprints_routes.)
    register_expert_packs_routes(app, deps)

    # ---- /v1/agents/* + /v1/sessions/{sid}/agent-overlay -------------
    # Tier-2 agent registry CRUD + list + extract and the session agent-overlay
    # routes (get/put/export) are owned by routes/agents.py; they reach the shared
    # row-resolution closures plus the destructive-action guard and workspace-
    # session mirror through ``deps``.
    register_agents_routes(app, deps)

    # ---- /v1/mcp/servers (#13) ---------------------------------------
    # MCP server registry + dispatch (list/detail/install/call/reconnect/
    # uninstall + tools/resources/prompts + handshake) are owned by
    # routes/mcp.py; the uninstall route reaches the destructive-action guard
    # through ``deps``.
    register_mcp_routes(app, deps)

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

    # ---- /v1/providers (#15) + /v1/providers/lm (CLIO-BBBBBBBBBB-D) ---
    # The LM-provider catalog (list/detail/auth/models/handshake) and the runtime
    # LM-bind routes (get/put/wait LM config) are owned by routes/providers.py. The
    # write-side bind hot-swaps the live agent's LMs and mutates
    # ``dspy.settings.main_thread_config`` + ``os.environ`` (snapshot/restore on
    # failure); it reaches the agent-rebuild hooks (install-tool-runtime-hooks /
    # clear-session-model-refs) through ``deps``.
    register_providers_routes(app, deps)

    # ---- 501 stubs for the still-unwired v0.2 surface ----------------

    _stub_routes: list[tuple[str, str, str]] = [
        # (method, path, capability_name_for_error)
        # /v1/tools moved out of stubs — implemented below.
    ]

    # ---- /v1/catalog/tools + /v1/tools + /v1/tools/{tool_id} ----------
    # The built-in tool catalog and the unified live catalog (bundled gateway +
    # installed third-party MCP servers) are owned by routes/catalog.py and
    # registered below via register_catalog_routes(app, deps).

    # ---- /v1/hooks (SPEC §6.17 declarative hooks) --------------------
    # Declarative event-hook CRUD is owned by routes/hooks.py; the
    # direct-destructive-action guard the delete route needs travels on
    # ``deps``. Distinct from clio_agent.runtime.hooks (in-process Python
    # hooks the framework fires on tool/message events).
    register_hooks_routes(app, deps)

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

    def _make_stub(cap: str):
        # Use a Request param so FastAPI doesn't try to validate
        # path/query/body params against the handler signature —
        # stubs take anything and return 501.
        async def _stub(request: Request) -> JSONResponse:
            body = _not_implemented(cap).model_dump(exclude_none=True)
            return JSONResponse(status_code=501, content=body)

        return _stub

    for method, path, cap in _stub_routes:
        app.add_api_route(
            path,
            _make_stub(cap),
            methods=[method],
            include_in_schema=False,
        )

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

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request, exc: RequestValidationError) -> JSONResponse:
        """Wrap FastAPI request validation failures in the GACT envelope."""

        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="validation_error",
                message="Request validation failed.",
                details={"errors": exc.errors()},
                recoverable=True,
            )
        )
        return JSONResponse(
            status_code=422,
            content=envelope.model_dump(exclude_none=True),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
        """Return a structured 500 for unexpected route failures."""

        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="internal_error",
                message="Unhandled server error.",
                details={
                    "original_error": type(exc).__name__,
                    "original_message": str(exc),
                },
                recoverable=False,
            )
        )
        return JSONResponse(
            status_code=500,
            content=envelope.model_dump(exclude_none=True),
        )

    # --- optional web UI (`clio web`): serve the built SPA bundle same-origin ---
    # Gated on CLIO_WEB_DIR so the default server (TUI / headless API) is byte-for-
    # byte unchanged unless web mode is explicitly enabled. Mounted LAST so every
    # /v1 API route (and /docs, /openapi.json) registered above takes precedence;
    # an SPA fallback serves index.html for unknown non-API paths so client-side
    # (history) routing works. The bundle's API calls are same-origin (relative
    # /v1/...), so no CORS/proxy is needed — this is the in-process equivalent of
    # the docker clio-web nginx setup.
    _web_dir = os.environ.get("CLIO_WEB_DIR", "").strip()
    if _web_dir and (Path(_web_dir) / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles
        from starlette.responses import FileResponse

        class _SPAStaticFiles(StaticFiles):
            async def get_response(self, path: str, scope: Any) -> Any:
                try:
                    return await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code == 404:
                        return FileResponse(Path(_web_dir) / "index.html")
                    raise

        app.mount("/", _SPAStaticFiles(directory=_web_dir, html=True), name="web")

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


def main() -> None:
    """Console-script entry point.

    When ``CLIO_LM_PROVIDER`` is set the real ``ClioAgent`` is
    instantiated + injected so POST /messages drives a real LM.
    Otherwise the module-level ``app`` (no agent wired) runs, which
    is fine for capability introspection but 503s on /messages.
    """

    import uvicorn

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

    # Resolve trace verbosity (file→env→default) and install the formatted log
    # handler for the server process, now that the environment is settled.
    trace.configure()

    # Always build a fresh app inside main() — the module-level
    # ``app`` symbol is intentionally lazy (see __getattr__ above) so
    # that just importing ``clio_agent.gact.app`` doesn't pay
    # build_app's cost. When the env requests an agent we set
    # want_agent so the lifespan startup task constructs ClioAgent
    # in the background — uvicorn binds the port immediately, beating
    # gact-tui's 3-second deploy probe. POST /messages 503s until
    # app.state.agent is stamped by the background task.
    app_to_run: FastAPI = build_app()
    if not args.no_agent and os.environ.get("CLIO_LM_PROVIDER"):
        app_to_run.state.want_agent = True

    uvicorn.run(
        app_to_run,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
