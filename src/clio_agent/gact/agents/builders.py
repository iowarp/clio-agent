"""Dynamic-agent / Agent-Blueprint DSPy module builders for the GACT server (#714).

This module owns the expert builders carved out of ``clio_agent.gact.app``. They
compile registered dynamic agents into concrete DSPy modules:

* prompt-only user agents (:func:`_build_prompt_user_agent_module`);
* tool-declaring user agents (:func:`_build_tool_user_agent_module`);
* Agent-Blueprint experts (:func:`_build_blueprint_dspy_module`) using predict,
  chain-of-thought, or ReAct modules.

Supporting machinery covers tool/LM resolution, telemetry, non-ReAct schema repair,
and child delegation. The retaining ReAct engine, resolution,
and prompt composition remain in sibling modules; cross-concern helpers load lazily
to preserve the strangler seam without a module cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import skill_runtime as _skill_runtime
from clio_agent.gact.agents import toolset_inventory
from clio_agent.gact.agents.auto_tools import build_auto_react_tools
from clio_agent.gact.agents.blueprint_tool_recording import (
    recorded_load_skill_tool as _recorded_load_skill_tool,
)
from clio_agent.gact.agents.blueprint_tool_recording import (
    recorded_spawn_skill_task_tool as _recorded_spawn_skill_task_tool,
)
from clio_agent.gact.agents.blueprint_tool_recording import (
    recording_blueprint_tool as _recording_blueprint_tool,
)
from clio_agent.gact.agents.composition import (
    _runtime_active_workspace_context,
    _runtime_dynamic_agent_children_context,
)
from clio_agent.gact.agents.declared_native_tools import resolve_declared_native_tools
from clio_agent.gact.agents.reactv2_submit import tool_names as _tool_names
from clio_agent.gact.agents.resolution import _active_workflow_state_schema
from clio_agent.gact.agents.runtime import (
    _retaining_react_cls,
)
from clio_agent.gact.agents.signatures import (
    _prompt_user_agent_signature,
    _tool_user_agent_signature,
)
from clio_agent.gact.agents.tool_executor_resolution import (
    resolve_active_base_agent_tool_executor,
)
from clio_agent.gact.events import Event
from clio_agent.gact.permission_gate import (
    _external_mcp_permission_context,
    _invoke_permission_gate,
)
from clio_agent.gact.runtime.context_tokens import _resolve_expert_context_window
from clio_agent.gact.runtime.globals import (
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _BlueprintTerminalWorkflowState,
    _emit_semantic_event,
    _llm_provider_payload,
    _TurnCancelled,
    _UnsupportedSessionAgent,
)
from clio_agent.gact.runtime.type_parsing import (
    _blueprint_module_kind,
    _parse_field_annotation,
    _structured_output_enabled,
)
from clio_agent.runtime import trace
from clio_agent.tools.mcp_runtime import wire_value

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema
    from clio_agent.providers.lm_spec import LMSpec
    from clio_agent.providers.resolver import ResolvedLMSpec


def _default_profile_spec(base_agent: Any) -> "LMSpec":
    """Return the default-profile :class:`LMSpec` an undeclared expert inherits.

    Reads the active per-app profile store (``app.state.provider_profiles`` — the
    immutable, RCU-swapped registry from design §3.4) when an app is bound;
    otherwise falls back to the boot agent's live ``_provider_config`` (or a fresh
    :func:`load_config_from_env` config) projected to a secret-free spec. That
    fallback is the byte-identical single-default-LM baseline (RULE 2), so a
    direct call with no active app resolves exactly as before.

    Args:
        base_agent: The owning agent; consulted for ``_provider_config`` only when
            no per-app profile store is available.

    Returns:
        The default-profile :class:`LMSpec` whose fields undeclared expert fields
        inherit.
    """
    from clio_agent.providers.lm_spec import spec_from_config  # noqa: PLC0415

    app = _ctx.active_app()
    store = getattr(getattr(app, "state", None), "provider_profiles", None) if app else None
    if store is not None:
        default = getattr(store, "default", None)
        if default is not None:
            return default
    base_config = getattr(base_agent, "_provider_config", None)
    if base_config is None:
        from clio_agent.config import load_config_from_env  # noqa: PLC0415

        base_config = load_config_from_env()
    return spec_from_config(base_config)


def _dynamic_agent_lm_config(base_agent: Any, agent_def: "AgentDef") -> "ResolvedLMSpec":
    """Resolve a registered dynamic agent's provider identity to a ``ResolvedLMSpec``.

    Builds a serializable :class:`~clio_agent.providers.lm_spec.LMSpec` from the
    ``AgentDef`` — inheriting every field it does not declare from the active
    default profile (:func:`_default_profile_spec`) — then delegates to
    :func:`~clio_agent.providers.resolver.resolve_endpoint_and_handshake`, the
    pure endpoint + cached-handshake half (design §3.3/§4). The credential is
    deliberately NOT resolved here; each expert ``forward()`` resolves it fresh
    via :meth:`~clio_agent.providers.resolver.ResolvedLMSpec.materialize` because
    tokens rotate mid-session.

    This drops the former ``same_provider`` gate: a cross-provider expert now
    authenticates its own provider and gets its own handshake-folded
    ``context_window`` / context-aware ``max_tokens`` / reasoning + tool flags,
    instead of the empty credentials and ``None`` context window the gate produced
    (design §2 "the gap"). The undeclared same-provider expert still resolves to
    the default profile, preserving the baseline.

    Args:
        base_agent: The owning agent (source of the default-profile fallback).
        agent_def: The registered dynamic agent's definition.

    Returns:
        A :class:`~clio_agent.providers.resolver.ResolvedLMSpec` — the key-less,
        handshake-populated skeleton plus any structured handshake-fallback
        reason. Call :meth:`ResolvedLMSpec.materialize` to get the runnable config.
    """
    from clio_agent.providers.lm_spec import build_spec  # noqa: PLC0415
    from clio_agent.providers.resolver import resolve_endpoint_and_handshake  # noqa: PLC0415

    default_spec = _default_profile_spec(base_agent)
    # The boot/default-profile credential the main agent runs (its resolved
    # ``_provider_config.api_key``). It is carried onto the DEFAULT-profile path so
    # an undeclared expert authenticates with the exact key the main agent uses —
    # even when that key came from the generic ``CLIO_LM_API_KEY`` boot var and the
    # provider-native var (e.g. ``OPENAI_API_KEY``) is unset or names a different
    # account (finding #1: RULE-2 main-works/experts-401 asymmetry). The
    # credential_ref/spec stay secret-free — this key never serializes; it is a
    # runtime resolution artifact threaded only when the expert resolves to the
    # boot provider's default profile.
    base_config = getattr(base_agent, "_provider_config", None)
    boot_provider = str(getattr(base_config, "provider", "") or "")
    boot_key = str(getattr(base_config, "api_key", "") or "")
    declared_provider = str(getattr(agent_def, "default_provider", "") or "")
    if declared_provider and declared_provider != default_spec.provider:
        # Cross-provider expert: the endpoint / model / credential-ref / transport
        # are provider-scoped, so inheriting the default provider's values would
        # point the new provider at the wrong endpoint (and a foreign credential).
        # Blank them — the resolver fills the new provider's PROVIDER_DEFAULTS and
        # its own default credential — while the provider-agnostic sampling params
        # still inherit. This preserves the old ``same_provider`` endpoint/model
        # semantics (design §2 the gap; §4).
        default_spec = replace(
            default_spec,
            provider=declared_provider,
            model="",
            api_base="",
            credential_ref="",
            transport="",
        )
    spec = build_spec(agent_def, default_spec)
    # Thread the boot credential only when the expert resolves to the boot
    # provider's default profile. Skip argonne: its default credential is a
    # short-lived Globus token re-minted fresh per call by the resolver, so a
    # captured boot token would go stale — the fresh resolution must win there.
    default_credential = (
        boot_key
        if (boot_key and spec.provider == boot_provider and boot_provider != "argonne")
        else ""
    )
    return resolve_endpoint_and_handshake(spec, default_credential=default_credential)


def _build_prompt_user_agent_module(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Build a DSPy module wrapper for a streamable prompt-only dynamic agent."""

    import dspy  # noqa: PLC0415

    from clio_agent.config import create_chat_adapter  # noqa: PLC0415
    from clio_agent.gact.app import (  # noqa: PLC0415
        _cancelled_error_info,
        _coerce_expert_handoff_rows,
    )
    from clio_agent.lm.hooked_lm import create_hooked_lm  # noqa: PLC0415
    from clio_agent.prompts import PromptRegistry  # noqa: PLC0415
    from clio_agent.providers.credentials import CredentialResolver  # noqa: PLC0415

    class PromptUserAgentModule(dspy.Module):
        def __init__(self, base_agent: Any, agent_def: "AgentDef") -> None:
            super().__init__()
            self.agent_def = agent_def
            # Per-expert provider identity as data; the credential is resolved
            # fresh per forward() via ``self._resolved_spec.materialize`` (design
            # §4). ``self.config`` is the init-time materialization kept for
            # compatibility (adapter/context-window reads).
            self._resolved_spec = _dynamic_agent_lm_config(base_agent, agent_def)
            self._cred_resolver = CredentialResolver()
            self.config = self._resolved_spec.materialize(self._cred_resolver)
            self._provider_config = self.config
            runtime = PromptRegistry().resolve("clio.runtime.prompt_user_agent")
            runtime_text = str(getattr(runtime, "text", "") or "").strip()
            agent_prompt = agent_def.system_prompt.strip() or agent_def.description
            app = _ctx.active_app()
            child_context = (
                _runtime_dynamic_agent_children_context(
                    app,
                    agent_def,
                    session_id=_ctx.active_session_id(),
                )
                if app is not None
                else ""
            )
            skill_rt = _skill_runtime.skill_runtime_for_agent(
                app, agent_def, session_id=_ctx.active_session_id()
            )
            self.system_prompt = "\n\n".join(
                part
                for part in (runtime_text, agent_prompt, child_context, skill_rt.bodies_block)
                if part
            )
            self.has_declared_children = bool(child_context.strip())
            self.answer_synthesizer = dspy.Predict(_prompt_user_agent_signature())

        def forward(
            self,
            question: str,
            session_id: str,
            session_mode: str = "edit",
            session_edit_mode: str = "diff",
            cancel_requested: Any | None = None,
            images: list[Any] | None = None,
            files: list[Any] | None = None,
        ) -> Any:
            _ = (
                session_mode,
                session_edit_mode,
            )  # P1.2 #1064: kept for a stable forward() signature; mode is surfaced upstream in turn.py enrichment (inject_plan_mode_reminder), not here.
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            # Resolve the credential fresh for this call (tokens rotate); the
            # dspy.context boundary itself is unchanged (design §4).
            cfg = self._resolved_spec.materialize(self._cred_resolver)
            with dspy.context(
                lm=create_hooked_lm(cfg),
                adapter=create_chat_adapter(cfg),
            ):
                result = self.answer_synthesizer(
                    system_prompt=self.system_prompt,
                    question=question,
                    images=list(images or []),
                    files=list(files or []),
                )
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            answer = str(getattr(result, "answer", "") or "")
            return dspy.Prediction(
                answer=answer,
                selected_expert=self.agent_def.id,
                routing_rationale=f"Session selected user agent {self.agent_def.id!r}.",
                route_source="user_agent",
                session_id=session_id,
                expert_handoffs=_coerce_expert_handoff_rows(
                    getattr(result, "expert_handoffs", None)
                ),
                error_info=None,
            )

    return PromptUserAgentModule(base_agent, agent_def)


async def _call_enabled_external_mcp_tool(
    app: Any,
    server_id: str,
    info: Mapping[str, Any],
    tool_name: str,
    tool_args: Mapping[str, Any],
    tool_annotations: Any = None,
) -> str:
    """Call an explicitly enabled external MCP tool for a dynamic agent."""

    observer_name = f"{info.get('name', 'ext')}.{tool_name}"
    # Reach the permission gate via the active app's state (installed turn gate, else
    # the build_app-stored factory) rather than importing ``_make_permission_gate``
    # from ``gact.app`` -- keeps this module off a module-load cycle (#714 DI seam).
    gate = getattr(app.state, "pending_permission_gate", None)
    if gate is None:
        gate = app.state.make_permission_gate()
    decision = _invoke_permission_gate(
        gate,
        observer_name,
        dict(tool_args),
        _external_mcp_permission_context(tool_annotations),
    )
    if decision != "allow":
        raise PermissionError(f"tool call {observer_name!r} denied by permission gate")

    # Execution path (#1106 + #1113): this dynamic-agent call dispatches call_tool, so
    # its client comes from make_elicitation_client — the single factory PLUS the
    # elicitation handler bound to THIS call's invocation (one client per call).
    from clio_agent.gact.elicitation_bridge import make_elicitation_client  # noqa: PLC0415
    from clio_agent.gact.mcp_apps import call_tool_result_to_observer  # noqa: PLC0415
    from clio_agent.tools.execution import notify_tool_observer  # noqa: PLC0415
    from clio_agent.tools.mcp_config import (  # noqa: PLC0415
        MCPTransportError,
        transport_from_spec,
    )
    from clio_agent.tools.mcp_errors import typed_mcp_call_error  # noqa: PLC0415

    spec = info.get("spec", {})
    try:
        transport = transport_from_spec(spec)
        client_ctx = make_elicitation_client(app, transport, server_id, tool_name)
    except MCPTransportError:
        raise RuntimeError(f"unknown stored MCP transport for {server_id}") from None
    except Exception:  # noqa: BLE001
        raise RuntimeError("fastmcp Client unavailable") from None

    tool_observer = getattr(app.state, "pending_tool_observer", None)
    if tool_observer is None:
        tool_observer = app.state.make_tool_observer()
    notify_tool_observer(tool_observer, observer_name, dict(tool_args), "started")
    try:
        async with client_ctx as client:
            from clio_agent.tools.mcp_header_mismatch import (  # noqa: PLC0415
                call_tool_with_header_retry,
            )

            result = await call_tool_with_header_retry(client, tool_name, dict(tool_args))
    except Exception as raw_exc:  # noqa: BLE001
        # #1114: typed translation first — the model never sees a raw SDK class/message.
        surfaced = typed_mcp_call_error(raw_exc, tool=tool_name) or raw_exc
        notify_tool_observer(
            tool_observer, observer_name, dict(tool_args), "completed", error=repr(surfaced)
        )
        raise surfaced from raw_exc
    content = getattr(result, "content", None) or []
    result_text = "\n".join(str(getattr(part, "text", part)) for part in content)
    if not result_text:
        data = getattr(result, "data", None)
        result_text = (
            json.dumps(data, sort_keys=True, default=str)
            if isinstance(data, Mapping)
            else str(data if data is not None else result)
        )
    observer_result = call_tool_result_to_observer(result)
    notify_tool_observer(
        tool_observer,
        observer_name,
        dict(tool_args),
        "completed",
        # Legacy text projection for the model; the durable observer gets the
        # machine-readable public MCP result (private `_meta` stays excluded).
        result=observer_result,
    )
    if content:
        return result_text
    data = getattr(result, "data", None)
    if data is not None:
        return json.dumps(data, default=str) if isinstance(data, Mapping) else str(data)
    return str(result)


def _run_external_mcp_tool_sync(
    app: Any,
    server_id: str,
    info: Mapping[str, Any],
    tool_name: str,
    tool_args: Mapping[str, Any],
    tool_annotations: Any = None,
) -> str:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _call_enabled_external_mcp_tool(
                app,
                server_id,
                info,
                tool_name,
                tool_args,
                tool_annotations,
            )
        )

    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(
                _call_enabled_external_mcp_tool(
                    app,
                    server_id,
                    info,
                    tool_name,
                    tool_args,
                    tool_annotations,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return str(result.get("value", ""))


def _enabled_external_mcp_dspy_tools(
    app: Any, requested_tools: list[str], sources: dict[str, str]
) -> dict[str, Any]:
    """Return DSPy Tool wrappers for enabled Agent Blueprint MCP tools."""

    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        boundary_observed_tool,
        mcp_tool_title,
    )

    requested = set(requested_tools)
    available: dict[str, Any] = {}
    installed = getattr(app.state, "external_mcp_servers", {}) or {}
    for server_id, info in installed.items():
        if not isinstance(info, Mapping):
            continue
        if str(info.get("status") or "") != "ready":
            continue
        for tool_row in info.get("tools") or []:
            if not isinstance(tool_row, Mapping):
                continue
            tool_name = str(tool_row.get("name") or tool_row.get("id") or "").strip()
            if not tool_name or tool_name not in requested or not bool(tool_row.get("enabled")):
                continue
            if str(tool_row.get("status") or "") != "ready":
                continue
            description = str(tool_row.get("description") or tool_name)
            title = mcp_tool_title(tool_row)  # #1188: Tool.title, else ToolAnnotations.title
            schema = tool_row.get("input_schema") or {}
            properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
            if not isinstance(properties, dict):
                properties = {}
            tool_annotations = tool_row.get("annotations")

            def tool_fn(
                _tool_name: str = tool_name,
                _server_id: str = str(server_id),
                _info: Mapping[str, Any] = info,
                _tool_annotations: Any = tool_annotations,
                **kwargs: Any,
            ) -> str:
                return _run_external_mcp_tool_sync(
                    app,
                    _server_id,
                    _info,
                    _tool_name,
                    kwargs,
                    _tool_annotations,
                )

            tool_fn.__name__ = tool_name
            tool_fn.__doc__ = description
            # ``_run_external_mcp_tool_sync`` notifies the observer itself, so
            # the construction is marked observed — the assembly seam must not
            # add a second notification (exactly-once). ``title`` carries the
            # upstream MCP tool's declared title (#1188), when present.
            available[tool_name] = boundary_observed_tool(
                tool_fn,
                name=tool_name,
                desc=description,
                args=properties,
                title=title,
            )
            toolset_inventory.register_tool_source(sources, tool_name, str(server_id))
    return available


def _active_base_agent_tool_executor(base_agent: Any) -> Any:
    """Return the base agent's tool executor for the active session workspace.

    Dynamic-agent (blueprint/expert) tools are bound to a concrete tool
    executor instance: the DSPy tool calls that executor's ``call_tool``
    directly, so the cwd of the executor's stdio MCP subprocesses is fixed at
    bind time. When a session workspace is bound, the active tool-execution
    contextvar carries its root, and the base agent exposes a per-workspace
    executor (``_active_tool_executor``) whose stdio MCPs spawn with
    ``cwd=<workspace root>``. Prefer that executor so staged artifacts land in
    the bound workspace; fall back to the default (no-cwd) ``tool_executor``
    when no workspace is active or the per-workspace seam is unavailable.
    """

    return resolve_active_base_agent_tool_executor(
        base_agent,
        app=_ctx.active_app(),
        session_id=_ctx.active_session_id() or _ctx.active_tool_session_id(),
        emit_downgrade=_emit_mcp_downgrade_events,
    )


def _emit_mcp_downgrade_events(executor: Any) -> None:
    """Surface any recorded era downgrade for ``executor``'s servers (#1201)."""

    app = _ctx.active_app()
    if app is None:
        return
    from clio_agent.gact.mcp_connection_observability import (  # noqa: PLC0415
        emit_downgrade_events_for_executor,
    )

    emit_downgrade_events_for_executor(app, _ctx.active_session_id(), executor)


def _resolve_declared_tools_with_on_demand_mount(
    tool_executor: Any, requested_tools: list[str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build this resolve's available-tool map, mounting ON DEMAND any
    requested tool whose DECLARED namespace was never listed yet (#1237).

    A namespace is DECLARED when it appears on the executor's
    ``_clio_namespace_specs`` map (stamped by ``ClioAgent._active_tool_executor``
    at gateway-build time -- #1237 Gap 1 means this is routinely non-empty on
    a cold workspace/blueprint, since activation mounts nothing eagerly). A
    process-wide listing cache can make a tool name available before this
    workspace owns the persistent namespace connection; both conditions are
    checked before a turn is released.
    Mounting goes through the session-readiness boundary, which joins the
    discovery layer's single-flight attempt, reports lifecycle updates over
    GACT, and applies a short increasing-wait retry ladder. A terminal failure
    is reported in the returned ``mount_failures`` map (namespace -> typed
    reason) for THIS resolve only -- it is never remembered, so a later
    resolve/call can still re-attempt after the underlying condition changes.
    """

    from clio_agent.gact.mcp_readiness import (  # noqa: PLC0415
        mount_failure_reason,
        mount_namespace_for_session,
        namespaces_requiring_preparation,
    )

    available_tools = {
        name: tool
        for tool in tool_executor.to_dspy_tools()
        for name in [str(getattr(tool, "name", "") or "")]
        if name
    }
    declared_specs: Mapping[str, Any] = getattr(tool_executor, "_clio_namespace_specs", None) or {}
    mount_failures: dict[str, str] = {}
    needed_namespaces = namespaces_requiring_preparation(
        tool_executor,
        requested_tools,
        available_tools,
        declared_specs,
    )
    merged_any = False
    for namespace in sorted(needed_namespaces):
        try:
            mounted_tools = mount_namespace_for_session(
                tool_executor,
                namespace,
                declared_specs[namespace],
            )
        except Exception as exc:  # noqa: BLE001 - typed + named, never cached (next call retries)
            mount_failures[namespace] = mount_failure_reason(exc)
            logger.warning(
                "on_demand_mount_failed namespace=%s reason=%s error=%s",
                namespace,
                mount_failures[namespace],
                exc,
            )
            continue
        if mounted_tools:
            merged_any = True
    if merged_any:
        available_tools = {
            name: tool
            for tool in tool_executor.to_dspy_tools()
            for name in [str(getattr(tool, "name", "") or "")]
            if name
        }
    return available_tools, mount_failures


def _dynamic_agent_tools(
    base_agent: Any, agent_def: "AgentDef", sources: dict[str, str]
) -> list[Any]:
    """Resolve the exact DSPy tools a tool-declaring dynamic agent may use."""

    requested_tools, available_tools, gateway_requested = resolve_declared_native_tools(
        agent_def, sources
    )
    tool_executor = None
    if gateway_requested:
        try:
            tool_executor = _active_base_agent_tool_executor(base_agent)
        except Exception as exc:  # noqa: BLE001 - preserve the typed agent boundary
            from clio_agent.gact.mcp_readiness import mount_failure_reason  # noqa: PLC0415

            resolution_failures = {"workspace_fleet": mount_failure_reason(exc)}
            logger.exception(
                "custom_agent_tool_executor_unavailable agent=%s tools=%s",
                agent_def.id,
                gateway_requested,
            )
            raise _UnsupportedSessionAgent(
                agent_def.id,
                reason="custom_agent_tool_executor_unavailable",
                tools=gateway_requested,
                mount_failures=resolution_failures,
            ) from exc
    mount_failures: dict[str, str] = {}
    if tool_executor is None or not hasattr(tool_executor, "to_dspy_tools"):
        if gateway_requested:
            raise _UnsupportedSessionAgent(
                agent_def.id,
                reason="custom_agent_tool_executor_unavailable",
                tools=gateway_requested,
            )
    else:
        gateway_tools, mount_failures = _resolve_declared_tools_with_on_demand_mount(
            tool_executor, gateway_requested
        )
        mounted = toolset_inventory.mounted_namespace_set(tool_executor)
        for name in gateway_tools:
            # prefix is real provenance only if mounted (finding [D]); else "gateway".
            prefix, sep, bare = name.partition("_")
            source = prefix if (sep and bare and prefix in mounted) else "gateway"
            toolset_inventory.register_tool_source(sources, name, source)
        available_tools.update(gateway_tools)
    app = _ctx.active_app()
    if app is not None:
        available_tools.update(_enabled_external_mcp_dspy_tools(app, gateway_requested, sources))
    missing_tools = [name for name in requested_tools if name not in available_tools]
    resolved_tools = [name for name in requested_tools if name in available_tools]
    if missing_tools and not resolved_tools:  # nothing to degrade to -- brick TYPED (#1228 D3)
        logger.warning(
            "custom_agent_tools_unavailable diagnostics agent=%s available=%s "
            "executor=%s federation=%s mount_failures=%s",
            agent_def.id,
            sorted(available_tools)[:30],
            type(tool_executor).__name__ if tool_executor is not None else None,
            "present"
            if getattr(base_agent, "_remote_mcp_federation", None) is not None
            else "ABSENT",
            mount_failures,
        )
        raise _UnsupportedSessionAgent(
            agent_def.id,
            reason="custom_agent_tools_unavailable",
            tools=missing_tools,
            mount_failures=mount_failures,
        )
    if missing_tools:  # >=1 resolved -- degrade typed-and-loud per tool (#1228 D3)
        toolset_inventory.record_tools_unavailable_degraded(
            app, agent_def.id, missing_tools, mount_failures=mount_failures
        )
    return [_recording_blueprint_tool(available_tools[name]) for name in resolved_tools]


def _invalid_tool_selection_from_exception(
    exc: BaseException,
    *,
    allowed_tools: Iterable[str],
) -> str:
    """Extract a rejected tool name from DSPy parser/validation errors."""

    allowed = {str(name).strip() for name in allowed_tools if str(name).strip()}
    message = str(exc)
    candidates: list[str] = []
    for pattern in (
        r"next_tool_name\s+with\s+value\s+[`'\"]?([^`'\"\s,\)]+)",
        r"tool_name\s+with\s+value\s+[`'\"]?([^`'\"\s,\)]+)",
        r"[`'\"]([^`'\"]+)[`'\"]\s+is\s+not\s+one\s+of\s+\(",
        r"invalid\s+tool\s+[`'\"]?([^`'\"\s,\)]+)",
    ):
        candidates.extend(match.group(1).strip() for match in re.finditer(pattern, message, re.I))
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:")
        if candidate and candidate not in allowed:
            return candidate
    return ""


def _emit_invalid_tool_selection_event(
    app: Any,
    sid: str,
    agent_def: "AgentDef",
    *,
    requested_tool: str,
    allowed_tools: Iterable[str],
    exc: BaseException,
) -> None:
    """Publish blocked invalid-tool selection evidence for live and durable traces."""

    allowed = sorted({str(name).strip() for name in allowed_tools if str(name).strip()})
    payload = {
        "agent_id": agent_def.id,
        "agent_title": agent_def.title,
        "requested_tool": requested_tool,
        "allowed_tools": allowed,
        "tool_executed": False,
        "recovery_status": "failed",
        "error_type": type(exc).__name__,
        "error_message": str(exc)[:1000],
        "error_full": str(exc),
    }
    summary = (
        f"Expert {agent_def.id!r} selected unavailable tool {requested_tool!r}; "
        "CLIO blocked execution."
    )
    if hasattr(getattr(app, "state", None), "bus"):
        app.state.bus.publish(
            Event(
                type="tool.selection.invalid",
                session_id=sid,
                payload={
                    **payload,
                    "turn_id": _active_semantic_turn_id(),
                    "trace_id": _active_semantic_trace_id(),
                },
            )
        )
    _emit_semantic_event(
        app,
        sid,
        "tool.selection.invalid",
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status="failed",
        summary=summary,
        actor={"agent_id": agent_def.id, "role": "expert"},
        subject={"requested_tool": requested_tool},
        blueprint={
            "source": agent_def.source,
            "agent_id": agent_def.id,
            "parent_id": agent_def.parent_id,
            "tier": agent_def.tier,
        },
        provider={
            "default_provider": agent_def.default_provider,
            "default_model": agent_def.default_model,
        },
        payload=payload,
    )


def _tool_user_agent_max_iters(agent_def: "AgentDef", *, declared_children: int = 0) -> int:
    """The react loop's iteration budget for this expert.

    #1226 D1b: UNLIMITED (``0``) default -- not the old #948 S4 scaling cap
    that starved a long orchestrator (L3 died at a turn budget mid-task). A
    cap is now only an explicit blueprint opt-in (``max_iters``).
    """
    from clio_agent.gact.app import _user_agent_int_param  # noqa: PLC0415

    del declared_children
    max_iters = _user_agent_int_param(agent_def, "max_iters", 0)
    if max_iters < 0:
        raise ValueError("user agent parameter 'max_iters' must be zero (unlimited) or positive")
    return max_iters


def _injected_workflow_state_field_type(schema: "WorkflowStateSchema") -> Any:
    """Annotation for the auto-injected ``workflow_state`` output (Consumer A, #648).

    The type is built FROM THE PACK SCHEMA, not hardcoded:

    * GENERIC schema (no declared sections) -> ``dict[str, Any]`` byte-identically,
      preserving the historical free-dict contract for domain-free packs.
    * A schema that declares its section vocabulary -> a nested pydantic model.
      Each declared section becomes ``Optional[<SectionModel>] = None``, and every
      ``SectionModel`` carries a single typed ``status: Optional[Literal[...]] = None``
      drawn from that section's ``status_ranks`` (sorted). ``extra="allow"`` at BOTH
      the top level (undeclared sections) and each section level (undeclared keys such
      as ``metadata_path``) keeps the strict adapter from rejecting an otherwise-correct
      run, while an out-of-vocabulary ``status`` for a declared section now fails
      validation (the typing is real).
    """

    if not schema.sections:
        return dict[str, Any]

    from pydantic import ConfigDict, create_model  # noqa: PLC0415

    section_fields: dict[str, Any] = {}
    for section_name, rule in schema.sections.items():
        statuses = tuple(sorted(rule.status_ranks))
        status_annotation: Any = Optional[Literal[statuses]] if statuses else Optional[str]  # type: ignore[valid-type]
        section_model = create_model(
            f"WorkflowStateSection_{section_name}",
            __config__=ConfigDict(extra="allow"),
            status=(status_annotation, None),
        )
        section_fields[section_name] = (Optional[section_model], None)
    return create_model(
        "InjectedWorkflowState",
        __config__=ConfigDict(extra="allow"),
        **section_fields,
    )


def _blueprint_runtime_signature(
    agent_def: "AgentDef",
    *,
    app: Any = None,
    include_images: bool = False,
    include_files: bool = False,
) -> Any:
    """Build a DSPy Signature from a blueprint's ordered signature fields.

    ``app`` lets a caller that already holds the live app thread it in explicitly
    for the per-app children cache (#770 Site 2); when omitted the live turn's
    ``active_app()`` is the source (reliable in-turn via the keystone).
    """

    import dspy  # noqa: PLC0415

    raw_signature = agent_def.signature if isinstance(agent_def.signature, Mapping) else {}
    raw_inputs = raw_signature.get("inputs") or raw_signature.get("input") or {}
    raw_outputs = raw_signature.get("outputs") or raw_signature.get("output") or {}

    def _field_declaration(name: str, raw: Any) -> tuple[str, str, Any]:
        if isinstance(raw, Mapping):
            desc = str(
                raw.get("description")
                or raw.get("desc")
                or raw.get("doc")
                or raw.get("help")
                or name
            )
            # Accept either {type: "..."} or {type, fields: {...}} for nested objects.
            spec = dict(raw)
            if "dtype" in spec and "type" not in spec:
                spec["type"] = spec["dtype"]
            return name, desc, _parse_field_annotation(spec, model_name=f"{agent_def.id}_{name}")
        return name, str(raw or name), str

    def _ordered_fields(
        value: Any,
        defaults: list[tuple[str, str, Any]],
    ) -> list[tuple[str, str, Any]]:
        if isinstance(value, Mapping):
            mapping_rows = [
                _field_declaration(str(k).strip(), v) for k, v in value.items() if str(k).strip()
            ]
            return mapping_rows or defaults
        if isinstance(value, list):
            list_rows: list[tuple[str, str, Any]] = []
            for item in value:
                if isinstance(item, Mapping):
                    name = str(item.get("name") or item.get("id") or "").strip()
                    if name:
                        list_rows.append(_field_declaration(name, item))
                else:
                    name = str(item).strip()
                    if name:
                        list_rows.append((name, name, str))
            return list_rows or defaults
        return defaults

    inputs = _ordered_fields(
        raw_inputs,
        [("system_prompt", "Runtime instructions", str), ("question", "User request", str)],
    )
    # A blueprint that declares its own inputs (e.g. just `question`) REPLACES the
    # defaults, which silently dropped the `system_prompt` input -- so the expert's
    # built system prompt (blueprint body + orchestrator briefing + workspace context,
    # assembled into runtime_system_prompt) was never passed to the model at all; it
    # ran on the generic 47-char signature instruction + the question only. Always
    # carry a `system_prompt` input so the body actually reaches the model. (Under the
    # contract state machine this was latent -- routing was deterministic -- but with
    # agent-driven routing the expert MUST see its own instructions to orchestrate.)
    if not any(name == "system_prompt" for name, _, _ in inputs):
        inputs = [
            ("system_prompt", "Runtime instructions and context for this expert", str),
            *inputs,
        ]
    if include_images and not any(name == "images" for name, _, _ in inputs):
        inputs.append(("images", "User-provided images for this turn", list[dspy.Image]))
    if include_files and not any(name == "files" for name, _, _ in inputs):
        inputs.append(("files", "User-provided PDF documents for this turn", list[dspy.File]))
    outputs = _ordered_fields(raw_outputs, [("answer", "User-facing answer", str)])
    structured = (
        agent_def.structured_outputs if isinstance(agent_def.structured_outputs, Mapping) else {}
    )

    # CLEAN CONTRACT: workflow_state is the ONE load-bearing structured output --
    # a TYPED dict the adapter forces the model to emit, and the channel the
    # agent->agent handoff actually travels on (carried STRUCTURALLY on every
    # Prediction / handoff row, never re-parsed from prose). The former companions
    # (evidence/errors/delegation) were a redundant second copy nothing authoritative
    # consumed (`evidence` merged-then-stripped from display; `errors` logging-only;
    # `delegation` clio builds itself). The legacy `artifacts` field was DELETED
    # wholesale in #969 -- the model now designates via the create_artifact tool, not
    # a typed output field; nothing injects or reads an inert artifacts structured key.
    # Declaring them *required* only enlarged the contract and made strict-adapter
    # models (nemotron's remote JSONAdapter path) hard-fail an otherwise-correct run
    # when they sensibly omitted an empty one -- so we no longer auto-inject them. (A
    # blueprint that genuinely needs one can still declare it in its `outputs:`.)
    # Resolve the session's active workflow_state schema once. Consumer A (#648): the injected
    # workflow_state field is TYPED FROM THE PACK SCHEMA -- a GENERIC (no declared
    # sections) schema keeps the historical free ``dict[str, Any]`` byte-identically,
    # while a pack that declares its vocabulary gets a nested pydantic model whose
    # per-section ``status`` is a real ``Optional[Literal[...]]`` (undeclared keys and
    # undeclared sections still validate via ``extra="allow"`` at both levels).
    _route_app = app if app is not None else _ctx.active_app()
    _route_sid = _ctx.active_session_id()
    # No try/except: the resolver already returns GENERIC for app-less/session-less
    # callers, so any exception here is a real defect that must propagate loudly
    # (matching every other call site of the resolver -- turn.py and the two
    # delegate/seed sites below). Swallowing it would silently downgrade the
    # injected annotation with no recorded reason (no-silent-fallback ground rule).
    _workflow_state_schema = _active_workflow_state_schema(_route_app, _route_sid)
    _workflow_state_field_type = _injected_workflow_state_field_type(_workflow_state_schema)
    _structured_field_specs: dict[str, tuple[str, Any]] = {
        "workflow_state": (
            "Typed semantic workflow state (a JSON object) used for blueprint continuation routing.",
            _workflow_state_field_type,
        ),
    }
    _declared = {field for field, _, _ in outputs}
    for name, (desc, field_type) in _structured_field_specs.items():
        if _structured_output_enabled(structured.get(name, True)) and name not in _declared:
            outputs.append((name, desc, field_type))

    # #948 S4: an orchestrator (an expert with declared children) routes by CALLING
    # the spawn-runtime tools (spawn_agent_task / wait_agent_tasks), not by emitting a
    # typed routing field consumed by a settle loop. The signature therefore carries
    # NO routing field — its children are reachable as real child turns via the spawn
    # tools attached in the react branch below.
    # (_route_app / _route_sid resolved above with the workflow_state schema.)

    # KEEP workflow_state TYPED (do not flatten to dict[str, Any]). The typed
    # pydantic field names are what ENFORCE the exact workflow_state keys the
    # continuation contracts route on (e.g. ``station_catalog.status``). When the
    # field was flattened to a free dict, a small model (qwopus) emitted the
    # ranking under the wrong key (``catalog`` instead of ``station_catalog``), so
    # ``station_catalog.status`` resolved to None and the data->resolver contract
    # never fired. Code-trained models still tend to emit this typed field as a
    # Python constructor-repr (``Model(field=...)``) rather than JSON; that is
    # recovered by the LenientChatAdapter (constructor-repr -> JSON, no re-request)
    # with DSPy's JSON-adapter fallback OFF for local backends so the recovery is
    # not bypassed. The recovery preserves the correct keys.
    if trace.HF_ON:
        trace.hot(
            "SIG-BUILD",
            "%s :: workflow_state type=%s",
            getattr(agent_def, "id", "?"),
            next((str(t) for n, d, t in outputs if n == "workflow_state"), "<none>"),
        )

    namespace: dict[str, Any] = {
        "__doc__": f"DSPy signature for Agent Blueprint expert {agent_def.id}."
    }
    annotations: dict[str, Any] = {}
    for name, desc, field_type in inputs:
        annotations[name] = field_type
        namespace[name] = dspy.InputField(desc=desc)
    for name, desc, field_type in outputs:
        annotations[name] = field_type
        # #948 S4: `answer` is REQUIRED again. The optional-with-default escape
        # hatch existed so a multi-round orchestrator could defer its deliverable
        # to a synthesis child; that world is deleted. A react main produces its
        # answer at extract (the finish deliverable IS the user answer), and a
        # leaf expert's answer is its whole return contract — an omitted answer
        # is a typed failure, never a legitimate state.
        namespace[name] = dspy.OutputField(desc=desc)
    namespace["__annotations__"] = annotations
    return type(
        f"{agent_def.id.title().replace('-', '').replace('_', '')}BlueprintSignature",
        (dspy.Signature,),
        namespace,
    )


def _emit_blueprint_llm_failure(agent_def: "AgentDef", kind: str, exc: BaseException) -> None:
    """Best-effort failure event retaining the ReAct trajectory in durable trace only."""

    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        return
    retained = _ctx.active_trajectory() if kind == "react" else None
    payload: dict[str, Any] = {
        # SSE/UI gets a summary; canonical trace keeps the uncapped exception.
        "error": str(exc).replace("\n", " ")[:2000],
        "error_full": str(exc),
        "error_type": type(exc).__name__,
    }
    if retained and retained.get("trajectory"):
        payload["trajectory"] = wire_value(retained.get("trajectory"), mode="gact_runtime")
    agent_id = str(getattr(agent_def, "id", "") or "")
    try:
        _emit_semantic_event(
            app,
            sid,
            "llm.response.failed",
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
            status="failed",
            summary=f"Expert {agent_id or '?'} extract failed: {type(exc).__name__}",
            actor={"agent_id": agent_id},
            provider=_llm_provider_payload(app, agent_id),
            payload=payload,
        )
    except Exception:  # noqa: BLE001,S110 - capture must never break the repair flow
        pass


def _build_blueprint_dspy_module(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Compile an Agent Blueprint expert into the DSPy module declared by module.kind."""

    import dspy  # noqa: PLC0415

    from clio_agent.config import create_chat_adapter  # noqa: PLC0415
    from clio_agent.gact.agents.module_variants import (  # noqa: PLC0415
        wrap_module_variant as _wrap_module_variant,
    )
    from clio_agent.gact.app import (  # noqa: PLC0415
        _cancelled_error_info,
        _coerce_expert_handoff_rows,
        _extract_tools_called_from_trajectory,
        _merge_tool_call_rows,
        _workflow_state_from_outputs,
    )
    from clio_agent.lm.hooked_lm import create_hooked_lm  # noqa: PLC0415
    from clio_agent.providers.credentials import CredentialResolver  # noqa: PLC0415

    class BlueprintExpertModule(dspy.Module):
        def __init__(self, base_agent: Any, agent_def: "AgentDef") -> None:
            super().__init__()
            self.agent_def = agent_def
            self.kind = _blueprint_module_kind(agent_def)
            # Per-expert provider identity as data; the credential is resolved
            # fresh per forward() via ``self._resolved_spec.materialize`` (design
            # §4). ``self.config`` is the init-time materialization kept for
            # compatibility (adapter/context-window reads).
            self._resolved_spec = _dynamic_agent_lm_config(base_agent, agent_def)
            self._cred_resolver = CredentialResolver()
            self.config = self._resolved_spec.materialize(self._cred_resolver)
            self._provider_config = self.config
            self.signature = _blueprint_runtime_signature(
                agent_def,
                include_images=True,
                include_files=True,
            )
            self.tools: list[Any] = []
            skill_rt = _skill_runtime.skill_runtime_for_agent(
                _ctx.active_app(), agent_def, session_id=_ctx.active_session_id()
            )
            if self.kind == "predict":
                self.program = dspy.Predict(self.signature)
            elif self.kind == "chain_of_thought":
                self.program = dspy.ChainOfThought(self.signature)
            else:
                # #948 S4: react mains route by SPAWNING declared children as real
                # child turns (spawn_agent_task / wait_agent_tasks / fanout); the
                # old inline per-child delegate/fan-out tools and the settle loop
                # are deleted. The spawn runtime re-emits the wire
                # blueprint.delegation.* events for TUI parity.
                from clio_agent.gact.agents.spawn_runtime import (  # noqa: PLC0415
                    build_spawn_runtime_tools,
                )

                _declared_tools = _dynamic_agent_tools(
                    base_agent, agent_def, (_sources := cast(dict[str, str], {}))
                )
                _spawn_tools = build_spawn_runtime_tools(
                    base_agent,
                    agent_def,
                    enable_skill_task_collection=_skill_runtime.skill_runtime_spawns_subagents(
                        skill_rt
                    ),
                )
                toolset_inventory.register_tool_sources(_sources, _spawn_tools, "spawn-runtime")
                tools = [
                    *_declared_tools,
                    *_spawn_tools,
                ]
                if skill_rt.resolved:
                    # Auto-attached infra (like child-delegation tools), not a
                    # curated domain tool (#919).
                    _skill_tool = _recorded_load_skill_tool(agent_def, skill_rt)
                    toolset_inventory.register_tool_sources(_sources, [_skill_tool], "native")
                    tools.append(_skill_tool)
                    if _skill_runtime.skill_runtime_spawns_subagents(skill_rt):
                        _spawn_skill_tool = _recorded_spawn_skill_task_tool(agent_def, skill_rt)
                        toolset_inventory.register_tool_sources(
                            _sources, [_spawn_skill_tool], "spawn-runtime"
                        )
                        tools.append(_spawn_skill_tool)
                # create_artifact (#969) + plan_exit (#1066) + write_todos (#1067): auto-attached.
                _auto_tools = build_auto_react_tools(agent_def)
                toolset_inventory.register_tool_sources(_sources, _auto_tools, "native")
                tools += _auto_tools
                # THE assembly seam (owner 2026-08-05): every tool is observed by
                # definition -- unmarked callables get the observer wrap + titles registered.
                from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
                    instrument_tools,
                )

                tools = instrument_tools(tools)
                self.tools = tools
                # Obs Tools tab "available" view: the REAL built toolset,
                # captured once here where it is actually in hand.
                toolset_inventory.emit_agent_toolset_recorded(agent_def, tools, _sources)
                # The iteration default scales with the declared children — an
                # orchestrator pays spawn+wait per child inside this loop (#948 S4).
                _n_children = 0
                _children_app = _ctx.active_app()
                if _children_app is not None:
                    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
                        _runtime_declared_child_ids,
                    )

                    _n_children = len(
                        _runtime_declared_child_ids(
                            _children_app,
                            agent_def.id,
                            session_id=_ctx.active_session_id(),
                        )
                    )
                self.program = _retaining_react_cls()(
                    self.signature,
                    tools=tools,
                    max_iters=_tool_user_agent_max_iters(agent_def, declared_children=_n_children),
                )
                # Tag the program so its ReAct loop attributes each step to this
                # expert on the highway (see _emit_react_step_event).
                self.program._clio_expert_id = agent_def.id
            # #948 S5: wrap the inner program (any kind) in the declared dspy.BestOfN /
            # Refine variant (no-op when unset; typed ValueError on an invalid decl).
            self.program = _wrap_module_variant(self.program, agent_def)
            agent_prompt = agent_def.system_prompt.strip() or agent_def.description
            active_app = _ctx.active_app()
            # Do not short-circuit on active_app is None: the streamed forward falls
            # back to the sync _run_blueprint_dspy_agent build, which has the session
            # but NOT _ACTIVE_GACT_APP -- the function returns the cached briefing on
            # that path so the orchestrator grounding never drops.
            child_context = _runtime_dynamic_agent_children_context(
                active_app,
                agent_def,
                session_id=_ctx.active_session_id(),
            )
            workspace_context = (
                _runtime_active_workspace_context(
                    active_app,
                    session_id=_ctx.active_session_id(),
                )
                if active_app is not None and self.kind == "react"
                else ""
            )
            # Progressive disclosure (#919): react experts get metadata + the
            # load_skill tool; tool-less predict/CoT experts get the bodies.
            skills_context = (
                skill_rt.prompt_block if self.kind == "react" else skill_rt.bodies_block
            )
            self.system_prompt = "\n\n".join(
                part
                for part in (agent_prompt, workspace_context, child_context, skills_context)
                if part
            )
            self.has_declared_children = bool(child_context.strip())
            if trace.HF_ON:
                import traceback as _pbtb  # noqa: PLC0415

                _pb_frames = [
                    ln.strip().split("\n")[0].split("/")[-1]
                    for ln in _pbtb.format_stack()
                    if "gact/app.py" in ln
                ]
                trace.hot(
                    "PROMPT-BUILD",
                    "%s :: kind=%s child_ctx=%d ORCH=%s sid=%r app=%s caller=%s",
                    getattr(agent_def, "id", "?"),
                    self.kind,
                    len(child_context),
                    "ORCHESTRATOR" in self.system_prompt,
                    _ctx.active_session_id(),
                    active_app is not None,
                    " <- ".join(reversed(_pb_frames[-4:])),
                )

        def forward(
            self,
            question: str,
            session_id: str,
            session_mode: str = "edit",
            session_edit_mode: str = "diff",
            cancel_requested: Any | None = None,
            images: list[Any] | None = None,
            files: list[Any] | None = None,
        ) -> Any:
            _ = (
                session_mode,
                session_edit_mode,
            )  # P1.2 #1064: kept for a stable forward() signature; mode is surfaced upstream in turn.py enrichment (inject_plan_mode_reminder), not here.
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            if trace.HF_ON:
                trace.hot("FWD-ENTER", "%s kind=%s", getattr(self.agent_def, "id", "?"), self.kind)
            runtime_system_prompt = self.system_prompt
            # Qwen-family models (qwopus), with thinking disabled, write free-form prose
            # instead of the structured field format and never emit a terminator, so they
            # generate unboundedly (→ truncation / >900s wedge). Tell them to output only
            # the required fields and stop. Env-gated so it rides with CLIO_LM_DISABLE_THINKING
            # and never touches the well-behaved remote models.
            from clio_agent.config import _thinking_disabled  # noqa: PLC0415

            if _thinking_disabled():
                runtime_system_prompt = (
                    runtime_system_prompt
                    + "\n\nOUTPUT DISCIPLINE: Produce ONLY the required output fields, each "
                    "filled in directly and once. Do NOT write a prose narrative, do NOT "
                    "restate your reasoning, do NOT repeat or re-explain fields. After the "
                    "last required field, STOP immediately."
                )
            active_app = _ctx.active_app()
            active_session_id = _ctx.active_session_id()
            if active_app is not None:
                runtime_child_context = _runtime_dynamic_agent_children_context(
                    active_app,
                    self.agent_def,
                    session_id=active_session_id,
                )
                if runtime_child_context:
                    if runtime_child_context not in runtime_system_prompt:
                        runtime_system_prompt = "\n\n".join(
                            part for part in (runtime_system_prompt, runtime_child_context) if part
                        )
            kwargs: dict[str, Any] = {"question": question}
            if "system_prompt" in self.signature.input_fields:
                kwargs["system_prompt"] = runtime_system_prompt
            if "images" in self.signature.input_fields:
                kwargs["images"] = list(images or [])
            if "files" in self.signature.input_fields:
                kwargs["files"] = list(files or [])
            if trace.HF_ON:
                trace.hot("FWD-A", "%s child-context+seed-done", getattr(self.agent_def, "id", "?"))
            blueprint_tool_rows: list[dict[str, Any]] = []
            if self.kind == "react":
                from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
                    _active_workflow_state_schema,
                )

                prior_workflow_state = _workflow_state_from_outputs(
                    [question, runtime_system_prompt],
                    schema=_active_workflow_state_schema(active_app, active_session_id),
                )
                if trace.HF_ON:
                    trace.hot(
                        "FWD-B", "%s workflow-state-parsed", getattr(self.agent_def, "id", "?")
                    )
                if prior_workflow_state:
                    blueprint_tool_rows.append(
                        {
                            "name": "clio_prior_workflow_state",
                            "args": {},
                            "ok": True,
                            "result": {},
                            "workflow_state": prior_workflow_state,
                            "telemetry_source": "blueprint_react_context_seed",
                        }
                    )
            blueprint_tool_rows_token = (
                _ctx.set_blueprint_tool_rows(blueprint_tool_rows) if self.kind == "react" else None
            )
            _agent_id_for_stream = str(getattr(self.agent_def, "id", "") or "")
            # ARC live-context-plane wiring (scope, session, window; harmless for
            # predict/CoT). #878: module.kind rides the token; the gate is never by field.
            _react_scope_token = _ctx.set_react_scope(_agent_id_for_stream, self.kind)
            _react_session_token = _ctx.set_react_session(active_session_id)
            _react_window_token = _ctx.set_react_window(_resolve_expert_context_window(self.config))
            _structured_outputs = (
                self.agent_def.structured_outputs
                if isinstance(self.agent_def.structured_outputs, Mapping)
                else {}
            )
            # An expert's typed ``answer`` streams live as a visible deliverable
            # unless it declares a typed ``workflow_state`` extract (whose value flows
            # to the return contract behind *show more*, not the visible answer lane).
            # DECLARATIVE via the shared truthiness helper (#736/C): a quoted author
            # error (workflow_state: "no"/"false") must NOT flip the gate — bool("no")
            # is True. ``or False`` coalesces the off-by-default falsy values
            # (absent/None/""/0) so only a real truthy value reaches it.
            _answer_visible = not _structured_output_enabled(
                _structured_outputs.get("workflow_state") or False
            )
            _visible_answer_token = _ctx.set_visible_answer_stream(_answer_visible)
            if trace.HF_ON:
                _ck_sp = kwargs.get("system_prompt", "")
                _ck_q = str(kwargs.get("question", ""))
                trace.hot(
                    "LM-CALL",
                    "%s :: sp_len=%d ORCH=%s | has_station_ids_in_q=%s SIO5_in_q=%s P472_in_q=%s | q_tail=%r",
                    getattr(self.agent_def, "id", "?"),
                    len(str(_ck_sp)),
                    "ORCHESTRATOR" in str(_ck_sp),
                    "station_ids" in _ck_q,
                    "SIO5" in _ck_q,
                    "P472" in _ck_q,
                    _ck_q[-500:],
                )
            try:
                # Resolve the credential fresh for this call (tokens rotate); the
                # dspy.context boundary below is unchanged (design §4).
                _fwd_config = self._resolved_spec.materialize(self._cred_resolver)
                adapter = create_chat_adapter(_fwd_config)
                try:
                    # track_usage installs the usage tracker so the live plane's
                    # auto-compaction can read the call's exact prompt_tokens.
                    with (
                        dspy.track_usage(),
                        dspy.context(lm=create_hooked_lm(_fwd_config), adapter=adapter),
                    ):
                        result = self.program(**kwargs)
                except _BlueprintTerminalWorkflowState as terminal_exc:
                    terminal_state = terminal_exc.result.get("clio_runtime", {}).get(
                        "workflow_state", {}
                    )
                    terminal_mapping = (
                        dict(terminal_state) if isinstance(terminal_state, Mapping) else {}
                    )
                    result = dspy.Prediction(
                        answer="The workflow reached a terminal typed state.",
                        workflow_state=terminal_mapping,
                        evidence=[terminal_exc.result],
                        errors=[],
                        delegation={},
                        trajectory=None,
                        tools_called=[],
                    )
                except Exception as exc:
                    _emit_blueprint_llm_failure(self.agent_def, self.kind, exc)
                    raise
            finally:
                # Single-var stack: each token captured the FULL context at its set,
                # so reset MUST unwind in strict reverse-LIFO of the sets
                # (window -> session -> scope -> rows) or an earlier reset would
                # restore a snapshot that predates the later sets. (#714)
                _ctx.reset(_visible_answer_token)
                _ctx.reset(_react_window_token)
                _ctx.reset(_react_session_token)
                _ctx.reset(_react_scope_token)
                if blueprint_tool_rows_token is not None:
                    _ctx.reset(blueprint_tool_rows_token)
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            answer = str(getattr(result, "answer", "") or "")
            tools_called: list[dict[str, Any]] = []
            if self.kind == "react":
                tools_called = _extract_tools_called_from_trajectory(
                    getattr(result, "trajectory", None)
                )
                if blueprint_tool_rows:
                    tools_called = _merge_tool_call_rows(tools_called, blueprint_tool_rows)
            handoff_rows = _coerce_expert_handoff_rows(getattr(result, "expert_handoffs", None))
            return dspy.Prediction(
                answer=answer,
                selected_expert=self.agent_def.id,
                routing_rationale=f"Session selected Agent Blueprint expert {self.agent_def.id!r}.",
                route_source="agent_blueprint",
                session_id=session_id,
                expert_handoffs=handoff_rows,
                workflow_state=getattr(result, "workflow_state", ""),
                evidence=getattr(result, "evidence", ""),
                errors=getattr(result, "errors", ""),
                delegation=getattr(result, "delegation", ""),
                trajectory=getattr(result, "trajectory", None),
                reasoning=getattr(result, "reasoning", ""),
                tools_called=tools_called,
                termination_reason=getattr(result, "termination_reason", ""),
                # #953 [5]: carry the variant winner stamp (else dropped here) to the turn.
                variant_selection=getattr(result, "variant_selection", None),
                error_info=None,
            )

    return BlueprintExpertModule(base_agent, agent_def)


def _build_tool_user_agent_module(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Build a DSPy ReAct wrapper for a streamable tool-declaring dynamic agent."""

    import dspy  # noqa: PLC0415

    from clio_agent.config import create_chat_adapter  # noqa: PLC0415
    from clio_agent.gact.app import (  # noqa: PLC0415
        _cancelled_error_info,
        _coerce_expert_handoff_rows,
        _extract_tools_called_from_trajectory,
    )
    from clio_agent.lm.hooked_lm import create_hooked_lm  # noqa: PLC0415
    from clio_agent.prompts import PromptRegistry  # noqa: PLC0415
    from clio_agent.providers.credentials import CredentialResolver  # noqa: PLC0415

    class ToolUserAgentModule(dspy.Module):
        def __init__(self, base_agent: Any, agent_def: "AgentDef") -> None:
            super().__init__()
            self.agent_def = agent_def
            self.kind = "react"  # always ReAct-with-extract; read by the #878 stream gate
            # Per-expert provider identity as data; the credential is resolved
            # fresh per forward() via ``self._resolved_spec.materialize`` (design
            # §4). ``self.config`` is the init-time materialization kept for
            # compatibility (adapter/context-window reads).
            self._resolved_spec = _dynamic_agent_lm_config(base_agent, agent_def)
            self._cred_resolver = CredentialResolver()
            self.config = self._resolved_spec.materialize(self._cred_resolver)
            self._provider_config = self.config
            self.tools = _dynamic_agent_tools(
                base_agent, agent_def, (_sources := cast(dict[str, str], {}))
            )
            skill_rt = _skill_runtime.skill_runtime_for_agent(
                _ctx.active_app(), agent_def, session_id=_ctx.active_session_id()
            )
            if skill_rt.resolved:
                # Same react tier-1 + load_skill contract as blueprint experts (#919).
                _skill_tool = _recorded_load_skill_tool(agent_def, skill_rt)
                toolset_inventory.register_tool_sources(_sources, [_skill_tool], "native")
                self.tools.append(_skill_tool)
            # create_artifact (#969) + plan_exit (#1066) + write_todos (#1067): auto-attached.
            _auto_tools = build_auto_react_tools(agent_def)
            toolset_inventory.register_tool_sources(_sources, _auto_tools, "native")
            self.tools += _auto_tools
            # THE assembly seam (owner 2026-08-05): same default-on instrumentation as above.
            from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
                instrument_tools,
            )

            self.tools = instrument_tools(self.tools)
            # Obs Tools tab "available" view: the REAL built toolset,
            # captured once here where it is actually in hand.
            toolset_inventory.emit_agent_toolset_recorded(agent_def, self.tools, _sources)
            runtime = PromptRegistry().resolve("clio.runtime.tool_user_agent")
            runtime_text = str(getattr(runtime, "text", "") or "").strip()
            agent_prompt = agent_def.system_prompt.strip() or agent_def.description
            app = _ctx.active_app()
            child_context = (
                _runtime_dynamic_agent_children_context(
                    app,
                    agent_def,
                    session_id=_ctx.active_session_id(),
                )
                if app is not None
                else ""
            )
            workspace_context = (
                _runtime_active_workspace_context(
                    app,
                    session_id=_ctx.active_session_id(),
                )
                if app is not None
                else ""
            )
            self.system_prompt = "\n\n".join(
                part
                for part in (
                    runtime_text,
                    agent_prompt,
                    workspace_context,
                    child_context,
                    skill_rt.prompt_block,
                )
                if part
            )
            # Use the retaining ReAct subclass so this path also runs the ARC
            # live-context plane (writes segments + reads its prompt from ARC).
            # NOTE: no `answer_synthesizer` here — the classic loop's
            # `react_agent.extract.predict` alias died with the v0.8.0 flip
            # (ReActV2 has no `extract`), and forward() never used it: the
            # stale assignment crashed EVERY tool-user-agent build under V2.
            self.react_agent = _retaining_react_cls()(
                _tool_user_agent_signature(),
                tools=self.tools,
                max_iters=_tool_user_agent_max_iters(agent_def),
            )

        def forward(
            self,
            question: str,
            session_id: str,
            session_mode: str = "edit",
            session_edit_mode: str = "diff",
            cancel_requested: Any | None = None,
            images: list[Any] | None = None,
            files: list[Any] | None = None,
        ) -> Any:
            _ = (
                session_mode,
                session_edit_mode,
            )  # P1.2 #1064: kept for a stable forward() signature; mode is surfaced upstream in turn.py enrichment (inject_plan_mode_reminder), not here.
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            # ARC live-context-plane wiring; #878: module.kind rides the scope token.
            _scope_id = str(getattr(self.agent_def, "id", ""))
            _react_scope_token = _ctx.set_react_scope(_scope_id, self.kind)
            _react_session_token = _ctx.set_react_session(session_id)
            _react_window_token = _ctx.set_react_window(_resolve_expert_context_window(self.config))
            try:
                # track_usage installs the tracker so auto-compaction can read each
                # call's exact prompt_tokens.
                # Resolve the credential fresh for this call (tokens rotate); the
                # dspy.context boundary itself is unchanged (design §4).
                cfg = self._resolved_spec.materialize(self._cred_resolver)
                with (
                    dspy.track_usage(),
                    dspy.context(
                        lm=create_hooked_lm(cfg),
                        adapter=create_chat_adapter(cfg),
                    ),
                ):
                    result = self.react_agent(
                        system_prompt=self.system_prompt,
                        question=question,
                        images=list(images or []),
                        files=list(files or []),
                    )
            except Exception as exc:
                app = _ctx.active_app()
                allowed_tools = _tool_names(self.tools)
                requested_tool = _invalid_tool_selection_from_exception(
                    exc,
                    allowed_tools=allowed_tools,
                )
                if app is not None and requested_tool:
                    _emit_invalid_tool_selection_event(
                        app,
                        session_id,
                        self.agent_def,
                        requested_tool=requested_tool,
                        allowed_tools=allowed_tools,
                        exc=exc,
                    )
                raise
            finally:
                # Reverse-LIFO reset of the single-var stack (window -> session ->
                # scope); see the blueprint-forward note. (#714)
                _ctx.reset(_react_window_token)
                _ctx.reset(_react_session_token)
                _ctx.reset(_react_scope_token)
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            answer = str(getattr(result, "answer", "") or "")
            tools_called = _extract_tools_called_from_trajectory(
                getattr(result, "trajectory", None)
            )
            return dspy.Prediction(
                answer=answer,
                selected_expert=self.agent_def.id,
                routing_rationale=f"Session selected tool user agent {self.agent_def.id!r}.",
                route_source="user_agent",
                session_id=session_id,
                expert_handoffs=_coerce_expert_handoff_rows(
                    getattr(result, "expert_handoffs", None)
                ),
                trajectory=getattr(result, "trajectory", None),
                tools_called=tools_called,
                termination_reason=getattr(result, "termination_reason", ""),
                error_info=None,
            )

    return ToolUserAgentModule(base_agent, agent_def)
