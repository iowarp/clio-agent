"""Dynamic-agent / Agent-Blueprint DSPy module builders for the GACT server (#714).

This module owns the *expert builders* carved out of ``clio_agent.gact.app``: the
factories that compile a registered dynamic agent (user/skill agent or Agent
Blueprint expert) into the concrete DSPy module that actually runs it --

* prompt-only user agents (:func:`_build_prompt_user_agent_module`),
* tool-declaring user agents (:func:`_build_tool_user_agent_module`),
* Agent-Blueprint experts of every ``module.kind``
  (:func:`_build_blueprint_dspy_module`: predict / chain_of_thought / react),

together with their supporting machinery: the runtime signature builder
(:func:`_blueprint_runtime_signature`), the LM-config / tool-resolution chain
(including enabled external-MCP tools and the blueprint-tool telemetry wrapper),
the bounded SCHEMA-REPAIR retry / re-extract / tool-intent-recovery helpers, and
the synchronous child-expert + bounded-fanout delegation tools.

The retaining ReAct engine these builders instantiate lives in
:mod:`clio_agent.gact.agents.runtime`. Agent/blueprint *resolution* and prompt
*composition* live in :mod:`clio_agent.gact.agents.resolution` /
:mod:`~clio_agent.gact.agents.composition`. Cross-concern helpers that still live
in the ``gact.app`` turn handler / workflow-state subsystem (tool-result
bounding, handoff-row coercion, workflow-state extraction, the runner-dispatch
wrappers ``_blueprint_runner_for_agent`` / ``_run_dynamic_agent_compat``) are
imported *lazily from* ``gact.app`` inside the functions that need them -- a
deliberate strangler seam that keeps this module free of a module-load cycle back
into ``gact.app`` until those concerns are extracted in later steps. The
permission gate / tool observer are reached through ``app.state`` factories
(``make_permission_gate`` / ``make_tool_observer``), never imported from
``gact.app``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.composition import (
    _runtime_active_workspace_context,
    _runtime_dynamic_agent_children_context,
)
from clio_agent.gact.agents.resolution import (
    _runtime_child_agent_rows,
    _runtime_declared_child_ids,
)
from clio_agent.gact.agents.runtime import (
    _prediction_structured_metadata,
    _retaining_react_cls,
)
from clio_agent.gact.events import Event
from clio_agent.gact.runtime.context_tokens import _resolve_expert_context_window
from clio_agent.gact.runtime.globals import (
    _EXPERT_CHILDREN_CACHE,
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _BlueprintTerminalWorkflowState,
    _emit_semantic_event,
    _jsonish,
    _llm_provider_payload,
    _tool_session_context,
    _TurnCancelled,
    _UnsupportedSessionAgent,
)
from clio_agent.gact.runtime.type_parsing import (
    _blueprint_module_kind,
    _parse_field_annotation,
)
from clio_agent.gact.workflow_state.merge import _merge_workflow_state_mapping
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef


def _dynamic_agent_lm_config(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Build a provider config for a registered dynamic agent."""
    from clio_agent.config import (  # noqa: PLC0415
        LMProviderConfig,
        load_config_from_env,
    )
    from clio_agent.gact.app import (  # noqa: PLC0415
        _user_agent_float_param,
        _user_agent_int_param,
    )

    base_config = getattr(base_agent, "_provider_config", None)
    if base_config is None:
        base_config = load_config_from_env()
    provider = agent_def.default_provider or base_config.provider
    same_provider = provider == base_config.provider
    params = agent_def.parameters if isinstance(agent_def.parameters, Mapping) else {}
    api_base = str(params.get("api_base") or (base_config.api_base if same_provider else ""))
    api_key = base_config.api_key if same_provider else ""
    new_config = LMProviderConfig(
        provider=provider,  # type: ignore[arg-type]
        api_base=api_base,
        model=agent_def.default_model or (base_config.model if same_provider else ""),
        api_key=api_key,
        temperature=_user_agent_float_param(agent_def, "temperature", base_config.temperature),
        max_tokens=_user_agent_int_param(agent_def, "max_tokens", base_config.max_tokens),
        planner_temperature=base_config.planner_temperature,
        planner_max_tokens=base_config.planner_max_tokens,
        codex_transport=base_config.codex_transport,
        thinking_budget=_user_agent_int_param(
            agent_def,
            "thinking_budget",
            base_config.thinking_budget,
        ),
    )
    # Propagate the handshake-discovered context window (init=False fields, so the
    # constructor above leaves them None). Without this the live plane's
    # auto-compaction has no denominator on the dynamic-agent path, since
    # apply_handshake is not called here.
    if same_provider and new_config.model == base_config.model:
        for _attr in ("context_window", "chosen_context"):
            _val = getattr(base_config, _attr, None)
            if _val:
                setattr(new_config, _attr, _val)
    return new_config


def _prompt_user_agent_signature() -> Any:
    """Return the DSPy signature used by prompt-only dynamic agents."""
    import dspy  # noqa: PLC0415

    class PromptUserAgentSignature(dspy.Signature):
        """Run a registered CLIO user agent using supplied runtime instructions."""

        system_prompt: str = dspy.InputField(desc="Registered agent instructions")
        question: str = dspy.InputField(desc="User message for this agent")
        answer: str = dspy.OutputField(desc="User-facing answer")
        expert_handoffs: str = dspy.OutputField(
            desc=(
                "JSON array of synchronous child expert delegations to execute next. "
                "Use [] when no child expert should be called."
            )
        )

    return PromptUserAgentSignature


def _build_prompt_user_agent_module(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Build a DSPy module wrapper for a streamable prompt-only dynamic agent."""

    import dspy  # noqa: PLC0415

    from clio_agent.config import (  # noqa: PLC0415
        create_chat_adapter,
        create_lm,
    )
    from clio_agent.gact.app import (  # noqa: PLC0415
        _cancelled_error_info,
        _coerce_expert_handoff_rows,
    )
    from clio_agent.prompts import PromptRegistry  # noqa: PLC0415

    class PromptUserAgentModule(dspy.Module):
        def __init__(self, base_agent: Any, agent_def: "AgentDef") -> None:
            super().__init__()
            self.agent_def = agent_def
            self.config = _dynamic_agent_lm_config(base_agent, agent_def)
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
            self.system_prompt = "\n\n".join(
                part for part in (runtime_text, agent_prompt, child_context) if part
            )
            self.has_declared_children = bool(child_context.strip())
            self.answer_synthesizer = dspy.Predict(_prompt_user_agent_signature())

        def forward(
            self,
            question: str,
            session_id: str,
            session_mode: str = "chat",
            session_edit_mode: str = "diff",
            cancel_requested: Any | None = None,
        ) -> Any:
            del session_mode, session_edit_mode
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            with dspy.context(
                lm=create_lm(self.config),
                adapter=create_chat_adapter(self.config),
            ):
                result = self.answer_synthesizer(
                    system_prompt=self.system_prompt,
                    question=question,
                )
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            answer = str(getattr(result, "answer", "") or "").strip()
            if not answer:
                if not self.has_declared_children:
                    raise RuntimeError(f"user agent {self.agent_def.id!r} returned an empty answer")
                return dspy.Prediction(
                    answer="",
                    selected_expert=self.agent_def.id,
                    routing_rationale=(
                        "Prompt agent returned an empty answer; CLIO will attempt "
                        "declared-child handoff repair."
                    ),
                    route_source="user_agent",
                    session_id=session_id,
                    expert_handoffs=[],
                    error_info=None,
                )
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


def _tool_user_agent_signature() -> Any:
    """Return the DSPy signature used by tool-declaring dynamic agents."""
    import dspy  # noqa: PLC0415

    class ToolUserAgentSignature(dspy.Signature):
        """Run a registered CLIO user agent using supplied tool runtime instructions."""

        system_prompt: str = dspy.InputField(desc="Registered agent instructions")
        question: str = dspy.InputField(desc="User message for this agent")
        answer: str = dspy.OutputField(desc="User-facing answer")
        expert_handoffs: str = dspy.OutputField(
            desc=(
                "JSON array of synchronous child expert delegations to execute next. "
                "Use [] when no child expert should be called."
            )
        )

    return ToolUserAgentSignature


async def _call_enabled_external_mcp_tool(
    app: Any,
    server_id: str,
    info: Mapping[str, Any],
    tool_name: str,
    tool_args: Mapping[str, Any],
) -> str:
    """Call an explicitly enabled external MCP tool for a dynamic agent."""

    observer_name = f"{info.get('name', 'ext')}.{tool_name}"
    # Reach the permission gate via the active app's state (the already-installed
    # turn gate, else the build_app-stored factory) instead of importing
    # ``_make_permission_gate`` from ``gact.app`` -- keeps this module off a
    # module-load cycle back into the monolith (#714 DI seam).
    gate = getattr(app.state, "pending_permission_gate", None)
    if gate is None:
        gate = app.state.make_permission_gate()
    decision = gate(observer_name, dict(tool_args))
    if decision != "allow":
        raise PermissionError(f"tool call {observer_name!r} denied by permission gate")

    try:
        from fastmcp import Client  # noqa: PLC0415
        from fastmcp.client.transports import (  # noqa: PLC0415
            StdioTransport,
            StreamableHttpTransport,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"fastmcp Client unavailable: {exc!r}") from exc

    spec = info.get("spec", {})
    if spec.get("transport") == "stdio":
        from clio_agent.tools.mcp_config import pdeathsig_wrapped_command  # noqa: PLC0415

        # Reap this external MCP child if the clio server dies hard (SIGKILL/OOM/
        # crash) -- there is no parent-death link otherwise, so it would orphan to
        # init. Mirrors transport_for() for the configured/clio-kit servers.
        cmd, cmd_args = pdeathsig_wrapped_command(spec["command"], spec.get("args") or [])
        transport = StdioTransport(command=cmd, args=cmd_args)
    elif spec.get("transport") in {"http", "streamable-http"}:
        transport = StreamableHttpTransport(url=spec["url"])  # type: ignore[assignment]
    else:
        raise RuntimeError(f"unknown stored MCP transport for {server_id}: {spec!r}")

    tool_observer = getattr(app.state, "pending_tool_observer", None)
    if tool_observer is None:
        tool_observer = app.state.make_tool_observer()
    if tool_observer is not None:
        try:
            tool_observer(observer_name, dict(tool_args), "started", None)
        except Exception:
            pass
    try:
        async with Client(transport) as client:
            result = await client.call_tool(tool_name, dict(tool_args))
    except Exception as exc:  # noqa: BLE001
        if tool_observer is not None:
            try:
                tool_observer(observer_name, dict(tool_args), "completed", repr(exc))
            except Exception:
                pass
        raise
    content = getattr(result, "content", None) or []
    result_text = "\n".join(str(getattr(part, "text", part)) for part in content)
    if not result_text:
        data = getattr(result, "data", None)
        result_text = (
            json.dumps(data, sort_keys=True, default=str)
            if isinstance(data, Mapping)
            else str(data if data is not None else result)
        )
    if tool_observer is not None:
        try:
            tool_observer(observer_name, dict(tool_args), "completed", None, result_text)
        except Exception:
            pass
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
) -> str:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _call_enabled_external_mcp_tool(app, server_id, info, tool_name, tool_args)
        )

    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(
                _call_enabled_external_mcp_tool(app, server_id, info, tool_name, tool_args)
            )
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return str(result.get("value", ""))


def _enabled_external_mcp_dspy_tools(app: Any, requested_tools: list[str]) -> dict[str, Any]:
    """Return DSPy Tool wrappers for enabled Agent Blueprint MCP tools."""

    import dspy  # noqa: PLC0415

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
            schema = tool_row.get("input_schema") or {}
            properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
            if not isinstance(properties, dict):
                properties = {}

            def tool_fn(
                _tool_name: str = tool_name,
                _server_id: str = str(server_id),
                _info: Mapping[str, Any] = info,
                **kwargs: Any,
            ) -> str:
                return _run_external_mcp_tool_sync(app, _server_id, _info, _tool_name, kwargs)

            tool_fn.__name__ = tool_name
            tool_fn.__doc__ = description
            available[tool_name] = dspy.Tool(
                func=tool_fn,
                name=tool_name,
                desc=description,
                args=properties,
            )
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

    resolver = getattr(base_agent, "_active_tool_executor", None)
    if callable(resolver):
        try:
            executor = resolver()
        except Exception:  # noqa: BLE001 - degrade to default executor
            executor = None
        if executor is not None:
            return executor
    return getattr(base_agent, "tool_executor", None)


def _dynamic_agent_tools(base_agent: Any, agent_def: "AgentDef") -> list[Any]:
    """Resolve the exact DSPy tools a tool-declaring dynamic agent may use."""

    requested_tools = [str(t).strip() for t in agent_def.tools if str(t).strip()]
    tool_executor = _active_base_agent_tool_executor(base_agent)
    if tool_executor is None or not hasattr(tool_executor, "to_dspy_tools"):
        if requested_tools:
            raise _UnsupportedSessionAgent(
                agent_def.id,
                reason="custom_agent_tool_executor_unavailable",
                tools=requested_tools,
            )
        available_tools: dict[str, Any] = {}
    else:
        available_tools = {
            str(getattr(tool, "name", "")): tool
            for tool in list(tool_executor.to_dspy_tools())
            if getattr(tool, "name", "")
        }
    app = _ctx.active_app()
    if app is not None:
        available_tools.update(_enabled_external_mcp_dspy_tools(app, requested_tools))
    missing_tools = [name for name in requested_tools if name not in available_tools]
    if missing_tools:
        raise _UnsupportedSessionAgent(
            agent_def.id,
            reason="custom_agent_tools_unavailable",
            tools=missing_tools,
        )
    return [_recording_blueprint_tool(available_tools[name]) for name in requested_tools]


def _recording_blueprint_tool(tool: Any) -> Any:
    """Wrap a DSPy tool so blueprint ReAct predictions retain tool evidence."""

    import dspy  # noqa: PLC0415

    from clio_agent.gact.app import (  # noqa: PLC0415
        _bounded_tool_call_result,
        _tool_result_is_error,
    )

    name = str(getattr(tool, "name", "") or "").strip()
    desc = str(getattr(tool, "desc", "") or getattr(tool, "__doc__", "") or name)
    args = getattr(tool, "args", None) or {}

    def call_tool(**kwargs: Any) -> Any:
        started_at = time.perf_counter()
        rows = _ctx.active_blueprint_tool_rows()
        try:
            result = tool(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, _BlueprintTerminalWorkflowState):
                raise
            if rows is not None:
                rows.append(
                    {
                        "name": name,
                        "args": dict(kwargs),
                        "ok": False,
                        "duration_ms": (time.perf_counter() - started_at) * 1000,
                        "error": str(exc),
                        "telemetry_source": "blueprint_react_tool_wrapper",
                    }
                )
            raise
        row_result = _bounded_tool_call_result(result)
        if rows is not None:
            rows.append(
                {
                    "name": name,
                    "args": dict(kwargs),
                    "ok": not _tool_result_is_error(result),
                    "duration_ms": (time.perf_counter() - started_at) * 1000,
                    "result": row_result,
                    "telemetry_source": "blueprint_react_tool_wrapper",
                }
            )
        return result

    call_tool.__name__ = name
    call_tool.__doc__ = desc
    return dspy.Tool(func=call_tool, name=name, desc=desc, args=args)


def _tool_names(tools: Iterable[Any]) -> list[str]:
    """Return stable tool names from DSPy tool-like objects."""

    names: list[str] = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "").strip()
        if name:
            names.append(name)
    return names


def _adapter_tool_intent_from_exception(
    exc: BaseException,
    *,
    allowed_tools: Iterable[str],
) -> dict[str, Any] | None:
    """Recover a typed tool intent emitted where DSPy expected final fields."""

    from clio_agent.gact.app import (  # noqa: PLC0415
        _json_objects_from_text,
    )

    allowed = {str(name).strip() for name in allowed_tools if str(name).strip()}
    if not allowed:
        return None
    message = str(exc)
    if "tool_name" not in message or "tool_args" not in message:
        return None
    for obj in _json_objects_from_text(message):
        if not isinstance(obj, Mapping):
            continue
        tool_name = str(obj.get("tool_name") or obj.get("name") or "").strip()
        if tool_name not in allowed:
            continue
        tool_args = obj.get("tool_args") or obj.get("args") or obj.get("arguments") or {}
        if not isinstance(tool_args, Mapping):
            tool_args = {}
        return {"tool_name": tool_name, "tool_args": dict(tool_args)}
    return None


def _call_recovered_dspy_tool(tool: Any, args: Mapping[str, Any]) -> Any:
    """Call a DSPy tool recovered from malformed ReAct adapter output."""

    if callable(tool):
        return tool(**dict(args))
    func = getattr(tool, "func", None)
    if callable(func):
        return func(**dict(args))
    raise TypeError(f"tool is not callable: {getattr(tool, 'name', '<unknown>')}")


def _extract_repair_attempts() -> int:
    """How many bounded SCHEMA-REPAIR retries to attempt after the first failure.

    Each retry is an INDEPENDENT sample (see _repair_temperature): dspy/Qwen note
    that at temperature 0 a retry is identical (greedy), so retries only help at
    temp>0 -- and a single retry leaves most recoverable cases on the table. Default
    3 -> at ~80%/attempt recovery, 1-0.2^3 ~ 99%. Override: CLIO_EXTRACT_REPAIR_ATTEMPTS.
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        n = int(
            conf.resolve(
                "limits.extract_repair_attempts",
                env="CLIO_EXTRACT_REPAIR_ATTEMPTS",
                default=3.0,
                cast=conf.as_float,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break a turn
        return 3
    return max(0, n)


def _repair_temperature(base: float, repair_index: int) -> float:
    """Temperature for SCHEMA-REPAIR retry ``repair_index`` (1-based).

    A retry must SAMPLE (vary) but must NOT raise format drift. clio runs LMs with
    cache disabled, so a retry at the SAME temp>0 already re-samples a fresh,
    independent output -- no temperature bump is needed for variation. Bumping temp
    was actively harmful: higher temp increases structured-format drift (more
    AdapterParseError / dropped fields), which is the opposite of what the
    parse-error class needs (verified: a +0.1/retry grind regressed SD vs the
    constant-temp baseline). So retries reuse the base temperature; the ONLY lift is
    off greedy decoding (temp 0), where every retry would otherwise be identical
    (dspy _warn_zero_temp_rollout) -- there we sample at a modest non-zero floor.
    """
    if repair_index <= 0 or base > 0.0:
        return base
    return 0.5


def _is_repairable_typed_output_error(exc: BaseException) -> bool:
    """Whether an expert's failure is a typed-output SCHEMA validation miss that a
    single re-ask can fix -- a required field was DROPPED or set null by a model
    that already has the data -- as opposed to a tool/runtime error. Detected by the
    pydantic / DSPy validation signature so it covers BOTH the strict remote
    JSONAdapter and the local lenient adapter."""

    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "validation error",  # pydantic header
        "field required",  # a required field was dropped
        "input should be",  # wrong type / null where a value is required
        "adapterparseerror",  # DSPy wrapper around a parse/validation miss
        "expected to find output fields",  # adapter could not locate a declared field
    )
    return any(m in text for m in markers)


def _typed_output_repair_hint(exc: BaseException) -> str:
    """Build ONE bounded-repair instruction from a typed-output validation error,
    fed back to the SAME expert so it re-emits with the field corrected. This is
    clio's documented "re-ask when something is missing" bounded repair -- NOT a
    default and NOT hiding: the model fixes its own drop using evidence it already
    has."""

    detail = str(exc).replace("\n", " ").strip()
    # Keep BOTH ends: the HEAD shows what the model actually produced (the rejected
    # response the adapter echoes), the TAIL shows the actionable diff the adapter
    # appends AFTER it ("Expected to find output fields ... Actual ..." / the missing
    # field name / "Field required"). A head-only truncation drops exactly the part
    # the model needs to self-correct.
    if len(detail) > 1600:
        detail = f"{detail[:1000]} […] {detail[-600:]}"
    return (
        "SCHEMA-REPAIR (your previous response was REJECTED by output validation). "
        "Here is exactly what you produced and why it was rejected:\n"
        f"{detail}\n"
        "Re-emit your COMPLETE response in the required format, fixing EXACTLY that: a "
        "REQUIRED output field was missing, null, or unparseable. Emit EVERY declared "
        "field with a correct, non-empty value consistent with the evidence you already "
        "gathered (e.g. a 'ranked' status MUST include the list of station ids you "
        "ranked; a required boolean must be true or false, never null). Do NOT add keys "
        "outside the declared schema, and do NOT drop any other required field."
    )


def _reextract_over_retained_trajectory(program: Any, hint: str) -> Any:
    """Re-run ONLY the dspy.ReAct extract over the RETAINED trajectory.

    The qwopus failure mode is a model that completes its tool loop but drops a
    required output field at the final extract. Re-running the WHOLE program
    repeats the (already-successful, expensive) tool loop and can loop forever.
    Instead, re-run just ``program.extract`` over the trajectory _RetainingReAct
    stashed before the failed extract (S4a), steering it with the repair hint.
    The evidence is reused; only the typed-output format is re-emitted.

    Returns a dspy.Prediction on success, or None if there is no retained
    trajectory / the program is not a ReAct / the re-extract itself fails (the
    caller then falls back to the bounded full re-ask).
    """

    retained = _ctx.active_trajectory()
    _traj = retained.get("trajectory") if isinstance(retained, dict) else None
    trace.event(
        "REEXTRACT",
        "retained=%s traj_keys=%d input_keys=%s",
        "none" if retained is None else "present",
        len(_traj) if isinstance(_traj, dict) else -1,
        list(retained.get("input_args", {}).keys()) if isinstance(retained, dict) else [],
    )
    if not retained or not retained.get("trajectory"):
        return None
    extract = getattr(program, "extract", None)
    format_trajectory = getattr(program, "_format_trajectory", None)
    if extract is None or not callable(format_trajectory):
        return None

    import dspy  # noqa: PLC0415

    trajectory = retained["trajectory"]
    input_args = dict(retained.get("input_args") or {})
    # Steer the re-extract with the repair hint via the question input field.
    if input_args.get("question"):
        input_args["question"] = f"{input_args['question']}\n\n{hint}"
    try:
        formatted = format_trajectory(trajectory)
        extract_pred = extract(**input_args, trajectory=formatted)
    except Exception:  # noqa: BLE001 - re-extract is best-effort; fall back to full re-ask
        return None
    return dspy.Prediction(trajectory=trajectory, **extract_pred)


def _recover_blueprint_react_tool_intent(
    *,
    tools: Iterable[Any],
    exc: BaseException,
) -> Any | None:
    """Execute a scoped ReAct tool intent that was emitted in final-output form."""

    import dspy  # noqa: PLC0415

    from clio_agent.gact.app import (  # noqa: PLC0415
        _tool_result_is_error,
        _tool_result_preview,
    )

    tools_by_name = {
        str(getattr(tool, "name", "") or "").strip(): tool
        for tool in tools
        if str(getattr(tool, "name", "") or "").strip()
    }
    intent = _adapter_tool_intent_from_exception(
        exc,
        allowed_tools=tools_by_name.keys(),
    )
    if intent is None:
        return None
    tool_name = str(intent["tool_name"])
    tool_args = dict(intent["tool_args"])
    started_at = time.perf_counter()
    try:
        result = _call_recovered_dspy_tool(tools_by_name[tool_name], tool_args)
        ok = not _tool_result_is_error(result)
        error = _tool_result_preview(result) if ok is False else None
    except BaseException as tool_exc:  # noqa: BLE001
        result = None
        ok = False
        error = str(tool_exc)
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    tool_row: dict[str, Any] = {
        "name": tool_name,
        "args": tool_args,
        "result": result,
        "ok": ok,
        "duration_ms": duration_ms,
        "telemetry_source": "react_adapter_tool_intent_recovery",
    }
    if error:
        tool_row["error"] = error
    workflow_state: dict[str, Any] = {}
    preview = _tool_result_preview(result).strip()
    status = "succeeded" if ok else "failed"
    answer_parts = [
        (
            "Recovered a malformed ReAct tool intent that was emitted as final "
            f"JSON and executed scoped tool `{tool_name}`; tool execution {status}."
        )
    ]
    if error:
        answer_parts.append(f"Tool error: {error}")
    if preview:
        answer_parts.append(f"Tool result:\n{preview}")
    trajectory = {
        "step_0_tool_name": tool_name,
        "step_0_tool_args": tool_args,
        "step_0_observation": result,
    }
    return dspy.Prediction(
        answer="\n\n".join(answer_parts),
        workflow_state=workflow_state,
        evidence=preview,
        artifacts="",
        errors=error or "",
        delegation="",
        trajectory=trajectory,
        tools_called=[tool_row],
        expert_handoffs=[],
    )


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


def _tool_user_agent_max_iters(agent_def: "AgentDef") -> int:
    from clio_agent.gact.app import _user_agent_int_param  # noqa: PLC0415

    max_iters = _user_agent_int_param(agent_def, "max_iters", 5)
    if max_iters <= 0:
        raise ValueError("user agent parameter 'max_iters' must be positive")
    return max_iters


def _blueprint_runtime_signature(agent_def: "AgentDef") -> Any:
    """Build a DSPy Signature from a blueprint's ordered signature fields."""

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
    outputs = _ordered_fields(raw_outputs, [("answer", "User-facing answer", str)])
    structured = (
        agent_def.structured_outputs if isinstance(agent_def.structured_outputs, Mapping) else {}
    )

    def _structured_output_enabled(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off", "disabled"}
        return value is not False

    # CLEAN CONTRACT: workflow_state is the ONE load-bearing structured output --
    # a TYPED dict the adapter forces the model to emit, and the channel the
    # agent->agent handoff actually travels on (_append_prediction_workflow_state
    # carries ONLY workflow_state to the next expert). The former companions
    # (evidence/artifacts/errors/delegation) were a redundant second copy that
    # nothing authoritative consumed: `artifacts` is tool-tracked on disk
    # (clio_sut._artifacts) and its handoff rides in workflow_state; `evidence`
    # was merged-then-stripped from display; `errors` was logging-only; clio
    # BUILDS `delegation` state itself and ignored the LM's field. Declaring them
    # *required* only enlarged the contract and made strict-adapter models (the
    # remote JSONAdapter path: nemotron) hard-fail an otherwise-correct run when
    # they sensibly omitted an empty one. A smaller contract is easier for every
    # model to satisfy -- so we no longer auto-inject them. (A blueprint that
    # genuinely needs one can still declare it explicitly in its signature
    # `outputs:`.)
    _structured_field_specs: dict[str, tuple[str, Any]] = {
        "workflow_state": (
            "Typed semantic workflow state (a JSON object) used for blueprint continuation routing.",
            dict[str, Any],
        ),
    }
    _declared = {field for field, _, _ in outputs}
    for name, (desc, field_type) in _structured_field_specs.items():
        if _structured_output_enabled(structured.get(name, True)) and name not in _declared:
            outputs.append((name, desc, field_type))

    # Agent-driven routing (replaces the deterministic continuation_contracts state
    # machine + prose heuristics). EVERY expert emits, at the end of its run, a typed
    # routing decision the settle traversal reads: the id of the ONE next child to
    # descend into, or "finish" to return to its parent. main's parent is the user, so
    # main's `answer` on "finish" is the final deliverable. This mirrors dspy.ReAct's
    # `next_tool_name: Literal[tools + "finish"]` -- a typed Literal is exactly what
    # makes a model fill it reliably (the old free-string `expert_handoffs` was always
    # emitted empty, so 100% of routing fell through to the contracts). The Literal is
    # built per-expert from its OWN declared children (a leaf gets Literal["finish"]).
    _route_app = _ctx.active_app()
    _route_sid = _ctx.active_session_id()
    _agent_id = getattr(agent_def, "id", "")
    _child_ids: list[str] = []
    if _route_app is not None and _route_sid:
        try:
            _child_ids = sorted(
                _runtime_declared_child_ids(_route_app, _agent_id, session_id=_route_sid)
            )
        except Exception:  # noqa: BLE001 - routing field is best-effort at sig-build
            _child_ids = []
    # Resolve-once-then-reuse via a process-global cache: some signature-build paths
    # carry NEITHER the app nor the session context, so resolve children live when we
    # can and fall back to the cache otherwise -- keeps next_expert's Literal correct
    # instead of collapsing to Literal["finish"] and forcing an immediate finish.
    if _agent_id:
        if _child_ids:
            _EXPERT_CHILDREN_CACHE[_agent_id] = _child_ids
        elif _agent_id in _EXPERT_CHILDREN_CACHE:
            _child_ids = list(_EXPERT_CHILDREN_CACHE[_agent_id])
    if "next_expert" not in _declared:
        _route_values = tuple(_child_ids) + ("finish",)
        _next_expert_type = Literal[_route_values]  # type: ignore[valid-type]
        outputs.append(
            (
                "next_expert",
                (
                    "Routing decision emitted when THIS expert's own work is complete: the "
                    "id of the ONE next child expert to run, or 'finish' to return control "
                    "to your parent (when you choose 'finish', put the final result in "
                    "`answer`). Must be EXACTLY one of: " + ", ".join(_route_values) + "."
                ),
                _next_expert_type,
            )
        )
    if "next_task" not in _declared:
        outputs.append(
            (
                "next_task",
                (
                    "The concrete task/question to hand to the child named in next_expert "
                    "(leave empty when next_expert='finish')."
                ),
                str,
            )
        )

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
            "%s :: workflow_state type=%s :: next_expert=%s",
            getattr(agent_def, "id", "?"),
            next((str(t) for n, d, t in outputs if n == "workflow_state"), "<none>"),
            next((str(t) for n, d, t in outputs if n == "next_expert"), "<none>"),
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
        namespace[name] = dspy.OutputField(desc=desc)
    namespace["__annotations__"] = annotations
    return type(
        f"{agent_def.id.title().replace('-', '').replace('_', '')}BlueprintSignature",
        (dspy.Signature,),
        namespace,
    )


def _blueprint_fanout_config(agent_def: "AgentDef") -> dict[str, Any]:
    """Normalize the Agent Blueprint fanout declaration."""

    raw = agent_def.fanout if isinstance(agent_def.fanout, Mapping) else {}
    enabled = raw.get("enabled", bool(raw)) if raw else False
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"", "false", "0", "no", "off", "disabled"}
    try:
        max_workers = int(raw.get("max_workers") or raw.get("workers") or raw.get("limit") or 1)
    except (TypeError, ValueError):
        max_workers = 1
    return {
        "enabled": bool(enabled),
        "max_workers": max(1, max_workers),
        "strategy": str(raw.get("strategy") or raw.get("mode") or "declared_children").strip()
        or "declared_children",
    }


def _coerce_fanout_child_ids(value: Any) -> list[str]:
    """Coerce a model-supplied child id selection into ordered ids."""

    if value in (None, "", [], ()):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _build_child_expert_tool(base_agent: Any, parent: "AgentDef", child: "AgentDef") -> Any:
    """Generate a synchronous child-expert DSPy tool for ReAct blueprint experts."""

    import dspy  # noqa: PLC0415

    from clio_agent.gact.app import (  # noqa: PLC0415
        _append_session_workflow_state_context,
        _blueprint_runner_for_agent,
        _compact_dynamic_delegation_output,
        _extract_tools_called,
        _merge_tool_call_rows,
        _prediction_workflow_state,
        _run_dynamic_agent_compat,
    )

    def delegate_child(question: str) -> str:
        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise RuntimeError("child expert tool requires an active CLIO app/session context")
        if child.parent_id != parent.id:
            raise RuntimeError(f"{child.id!r} is not a declared child of {parent.id!r}")
        app_state = getattr(app, "state", None)

        _emit_semantic_event(
            app,
            session_id,
            "blueprint.delegation.started",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="running",
            summary=f"{parent.id} delegated to {child.id}",
            actor={"agent_id": parent.id, "role": "parent_expert"},
            subject={"agent_id": child.id, "role": "child_expert"},
            blueprint={
                "agent_blueprint_id": parent.metadata.get("agent_blueprint_id") or "",
                "parent_expert": parent.id,
                "child_expert": child.id,
            },
        )
        ledger_start = 0
        ledger = getattr(app_state, "tool_call_ledger", None)
        if isinstance(ledger, dict):
            session_rows = ledger.get(session_id)
            if isinstance(session_rows, list):
                ledger_start = len(session_rows)
        runner = _blueprint_runner_for_agent(child)
        delegated_question = _append_session_workflow_state_context(
            app,
            session_id,
            question,
        )
        try:
            with _tool_session_context(session_id):
                pred = _run_dynamic_agent_compat(
                    runner,
                    base_agent,
                    child,
                    delegated_question,
                    session_id,
                    None,
                )
        except Exception:
            _emit_semantic_event(
                app,
                session_id,
                "blueprint.delegation.failed",
                turn_id=_active_semantic_turn_id(),
                trace_id=_active_semantic_trace_id(),
                status="failed",
                summary=f"{parent.id} delegation to {child.id} failed",
                actor={"agent_id": parent.id, "role": "parent_expert"},
                subject={"agent_id": child.id, "role": "child_expert"},
                blueprint={
                    "agent_blueprint_id": parent.metadata.get("agent_blueprint_id") or "",
                    "parent_expert": parent.id,
                    "child_expert": child.id,
                },
            )
            raise
        output = str(getattr(pred, "answer", "") or "").strip()
        tools_called = _extract_tools_called(pred)
        if isinstance(ledger, dict):
            session_rows = ledger.get(session_id)
            if isinstance(session_rows, list) and len(session_rows) > ledger_start:
                tools_called = _merge_tool_call_rows(
                    tools_called,
                    [dict(row) for row in session_rows[ledger_start:] if isinstance(row, Mapping)],
                )
        # Seed from the child's typed workflow_state output field (structural twin
        # of the removed prose append); merge tool-row state. State rides the
        # payload's ``workflow_state`` Mapping below, NOT the output text.
        workflow_state = _prediction_workflow_state(pred)
        for tool_row in tools_called:
            row_state = tool_row.get("workflow_state")
            if isinstance(row_state, Mapping):
                _merge_workflow_state_mapping(workflow_state, row_state)
        compact_output = _compact_dynamic_delegation_output(output)
        payload = {
            "agent_id": child.id,
            "parent_id": parent.id,
            "status": "completed",
            "stage": "delegate.completed",
            "return_to": parent.id,
            "return_payload": "compact_result",
            "output_summary": compact_output,
            "workflow_state": workflow_state,
            "tools_called": tools_called,
            "structured": _prediction_structured_metadata(pred),
        }
        _emit_semantic_event(
            app,
            session_id,
            "blueprint.delegation.completed",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            summary=f"{child.id} returned compact evidence to {parent.id}",
            actor={"agent_id": child.id, "role": "child_expert"},
            subject={"agent_id": parent.id, "role": "parent_expert"},
            blueprint={
                "agent_blueprint_id": parent.metadata.get("agent_blueprint_id") or "",
                "parent_expert": parent.id,
                "child_expert": child.id,
            },
            # Canonical trace carries the FULL child output; the tool's RETURN
            # value (json.dumps(payload) below) keeps the compact output_summary
            # for the parent prompt.
            payload={**payload, "output": output},
        )
        return json.dumps(payload, sort_keys=True, default=str)

    safe_child_id = re.sub(r"[^A-Za-z0-9_]+", "_", child.id).strip("_") or "child"
    delegate_child.__name__ = f"delegate_to_{safe_child_id}"
    delegate_child.__doc__ = (
        f"Run declared child expert {child.id} synchronously and return compact evidence."
    )
    return dspy.Tool(
        func=delegate_child,
        name=delegate_child.__name__,
        desc=delegate_child.__doc__,
        args={
            "question": {
                "type": "string",
                "description": f"Specific task for child expert {child.id}.",
            }
        },
    )


def _build_fanout_tool(base_agent: Any, parent: "AgentDef", children: list["AgentDef"]) -> Any:
    """Generate a bounded fanout runtime primitive for blueprint ReAct experts."""

    import dspy  # noqa: PLC0415

    from clio_agent.gact.app import (  # noqa: PLC0415
        _blueprint_runner_for_agent,
        _compact_dynamic_delegation_output,
        _run_dynamic_agent_compat,
    )

    config = _blueprint_fanout_config(parent)
    max_workers = int(config["max_workers"])
    children_by_id = {child.id: child for child in children}

    def fanout_to_children(question: str, child_ids: Any = None) -> str:
        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise RuntimeError("fanout tool requires an active CLIO app/session context")
        requested = _coerce_fanout_child_ids(child_ids) or [child.id for child in children]
        unknown = [child_id for child_id in requested if child_id not in children_by_id]
        if unknown:
            raise RuntimeError(f"fanout requested undeclared child experts: {', '.join(unknown)}")
        selected = [children_by_id[child_id] for child_id in requested[:max_workers]]
        skipped = requested[max_workers:]
        _emit_semantic_event(
            app,
            session_id,
            "blueprint.fanout.started",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="running",
            summary=f"{parent.id} fanout started with {len(selected)} worker(s)",
            actor={"agent_id": parent.id, "role": "fanout_parent"},
            subject={"child_agent_ids": [child.id for child in selected]},
            blueprint={
                "agent_blueprint_id": parent.metadata.get("agent_blueprint_id") or "",
                "parent_expert": parent.id,
                "fanout": config,
            },
            payload={"requested_child_agent_ids": requested, "skipped_child_agent_ids": skipped},
        )
        results: list[dict[str, Any]] = []
        status = "completed"
        for child in selected:
            try:
                runner = _blueprint_runner_for_agent(child)
                pred = _run_dynamic_agent_compat(
                    runner, base_agent, child, question, session_id, None
                )
                results.append(
                    {
                        "agent_id": child.id,
                        "status": "completed",
                        # FULL output for the canonical trace; output_summary is the
                        # compact projection for the parent.
                        "output": str(getattr(pred, "answer", "") or ""),
                        "output_summary": _compact_dynamic_delegation_output(
                            str(getattr(pred, "answer", "") or "")
                        ),
                        "structured": _prediction_structured_metadata(pred),
                    }
                )
            except Exception as exc:  # pragma: no cover - exercised by state-space tests
                status = "partial_failure"
                results.append(
                    {
                        "agent_id": child.id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
        payload = {
            "agent_id": parent.id,
            "status": status,
            "stage": "fanout.completed",
            "max_workers": max_workers,
            "requested_child_agent_ids": requested,
            "executed_child_agent_ids": [row["agent_id"] for row in results],
            "skipped_child_agent_ids": skipped,
            "return_payload": "compact_results",
            "results": results,
        }
        _emit_semantic_event(
            app,
            session_id,
            "blueprint.fanout.completed",
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status=status,
            summary=f"{parent.id} fanout completed with {len(results)} result(s)",
            actor={"agent_id": parent.id, "role": "fanout_parent"},
            subject={"child_agent_ids": [row["agent_id"] for row in results]},
            blueprint={
                "agent_blueprint_id": parent.metadata.get("agent_blueprint_id") or "",
                "parent_expert": parent.id,
                "fanout": config,
            },
            payload={
                "executed_child_agent_ids": payload["executed_child_agent_ids"],
                "skipped_child_agent_ids": skipped,
                "result_count": len(results),
            },
        )
        return json.dumps(payload, sort_keys=True, default=str)

    fanout_to_children.__name__ = "fanout_to_children"
    fanout_to_children.__doc__ = (
        "Run a bounded set of declared child experts and return compact evidence from each."
    )
    return dspy.Tool(
        func=fanout_to_children,
        name="fanout_to_children",
        desc=fanout_to_children.__doc__,
        args={
            "question": {
                "type": "string",
                "description": "Task to run across selected child experts.",
            },
            "child_ids": {
                "type": "string",
                "description": "Optional JSON array or comma-separated declared child expert ids.",
            },
        },
    )


def _dynamic_child_expert_tools(base_agent: Any, agent_def: "AgentDef") -> list[Any]:
    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    if app is None or not session_id:
        return []
    rows = _runtime_child_agent_rows(app, agent_def.id, session_id=session_id)
    tools = [_build_child_expert_tool(base_agent, agent_def, child) for child in rows]
    if rows and _blueprint_fanout_config(agent_def)["enabled"]:
        tools.append(_build_fanout_tool(base_agent, agent_def, rows))
    return tools


def _emit_blueprint_llm_failure(agent_def: "AgentDef", kind: str, exc: BaseException) -> None:
    """Emit ``llm.response.failed`` carrying the retained ReAct trajectory.

    Captures the one event stock dspy throws away: an expert that ran its tool
    loop but failed the final typed-output extract. The retained trajectory rides
    on the event so the canonical trace shows exactly what the model produced
    before the drop -- and so the repair path can re-run extract over it. The
    trajectory is in SENSITIVE_KEYS, so SSE strips it while the durable trace
    keeps it. Best-effort: never let capture interfere with the repair flow.
    """

    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        return
    retained = _ctx.active_trajectory() if kind == "react" else None
    payload: dict[str, Any] = {
        # `error` is a one-line summary for SSE/UI; `error_full` is the FULL,
        # uncapped exception (with newlines) for the canonical trace -- never cap.
        "error": str(exc).replace("\n", " ")[:2000],
        "error_full": str(exc),
        "error_type": type(exc).__name__,
        "repairable": bool(_is_repairable_typed_output_error(exc)),
    }
    if retained and retained.get("trajectory"):
        payload["trajectory"] = _jsonish(retained.get("trajectory"))
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
    except Exception:  # noqa: BLE001 - capture must never break the repair flow
        pass


def _build_blueprint_dspy_module(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Compile an Agent Blueprint expert into the DSPy module declared by module.kind."""

    import dspy  # noqa: PLC0415

    from clio_agent.config import create_chat_adapter, create_lm  # noqa: PLC0415
    from clio_agent.gact.app import (  # noqa: PLC0415
        _cancelled_error_info,
        _coerce_expert_handoff_rows,
        _extract_tools_called_from_trajectory,
        _merge_tool_call_rows,
        _tool_agent_empty_answer_fallback,
        _workflow_state_from_outputs,
    )

    class BlueprintExpertModule(dspy.Module):
        def __init__(self, base_agent: Any, agent_def: "AgentDef") -> None:
            super().__init__()
            self.agent_def = agent_def
            self.kind = _blueprint_module_kind(agent_def)
            self.config = _dynamic_agent_lm_config(base_agent, agent_def)
            self._provider_config = self.config
            self.signature = _blueprint_runtime_signature(agent_def)
            self.tools: list[Any] = []
            if self.kind == "predict":
                self.program = dspy.Predict(self.signature)
            elif self.kind == "chain_of_thought":
                self.program = dspy.ChainOfThought(self.signature)
            else:
                tools = [
                    *_dynamic_agent_tools(base_agent, agent_def),
                    *_dynamic_child_expert_tools(base_agent, agent_def),
                ]
                self.tools = tools
                self.program = _retaining_react_cls()(
                    self.signature,
                    tools=tools,
                    max_iters=_tool_user_agent_max_iters(agent_def),
                )
                # Tag the program so its ReAct loop attributes each step to this
                # expert on the highway (see _emit_react_step_event).
                self.program._clio_expert_id = agent_def.id
            agent_prompt = agent_def.system_prompt.strip() or agent_def.description
            active_app = _ctx.active_app()
            # Always call it (do not short-circuit on active_app is None): the streamed
            # forward falls back to the sync _run_blueprint_dspy_agent build, which has
            # the session but NOT _ACTIVE_GACT_APP -- the function returns the cached
            # briefing on that path so the orchestrator grounding never drops.
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
            self.system_prompt = "\n\n".join(
                part for part in (agent_prompt, workspace_context, child_context) if part
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
            session_mode: str = "chat",
            session_edit_mode: str = "diff",
            cancel_requested: Any | None = None,
        ) -> Any:
            del session_mode, session_edit_mode
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
            if os.environ.get("CLIO_LM_DISABLE_THINKING", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }:
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
            kwargs = {"question": question}
            if "system_prompt" in self.signature.input_fields:
                kwargs["system_prompt"] = runtime_system_prompt
            if trace.HF_ON:
                trace.hot("FWD-A", "%s child-context+seed-done", getattr(self.agent_def, "id", "?"))
            blueprint_tool_rows: list[dict[str, Any]] = []
            if self.kind == "react":
                prior_workflow_state = _workflow_state_from_outputs(
                    [question, runtime_system_prompt]
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
            # ARC live-context-plane wiring for this expert's ReAct loop: the scope
            # (the agent/expert tag), owning session, and the context window (the
            # auto-compaction denominator). Only the react kind runs _RetainingReAct,
            # but setting them unconditionally is harmless (predict/CoT never read).
            _react_scope_token = _ctx.set_react_scope(str(getattr(self.agent_def, "id", "")))
            _react_session_token = _ctx.set_react_session(active_session_id)
            _react_window_token = _ctx.set_react_window(_resolve_expert_context_window(self.config))
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
                adapter = create_chat_adapter(self.config)
                _base_temp = float(getattr(self.config, "temperature", 0.0) or 0.0)
                _max_repairs = _extract_repair_attempts()
                _repair_hint = ""
                # original attempt + up to _max_repairs bounded SCHEMA-REPAIR retries
                for _repair_attempt in range(1 + _max_repairs):
                    # Per-attempt temperature: the original keeps the configured base;
                    # each retry bumps temp so it is a genuinely INDEPENDENT sample (a
                    # temp-0 retry reproduces the same greedy output and cannot recover
                    # -- dspy _warn_zero_temp_rollout).
                    _attempt_temp = _repair_temperature(_base_temp, _repair_attempt)
                    _attempt_config = (
                        self.config
                        if _attempt_temp == _base_temp
                        else replace(self.config, temperature=_attempt_temp)
                    )
                    _call_kwargs = (
                        kwargs
                        if not _repair_hint
                        else {**kwargs, "question": f"{kwargs['question']}\n\n{_repair_hint}"}
                    )
                    try:
                        # track_usage installs the usage tracker so the live plane's
                        # auto-compaction can read each call's exact prompt_tokens.
                        with (
                            dspy.track_usage(),
                            dspy.context(lm=create_lm(_attempt_config), adapter=adapter),
                        ):
                            result = self.program(**_call_kwargs)
                        break
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
                            artifacts=[],
                            errors=[],
                            delegation={},
                            trajectory=None,
                            tools_called=[],
                        )
                        break
                    except Exception as exc:
                        # Capture the failed extract WITH its retained trajectory before
                        # anything else, so the canonical trace records what the model
                        # produced even when the repair recovers it.
                        _emit_blueprint_llm_failure(self.agent_def, self.kind, exc)
                        _eid = getattr(self.agent_def, "id", "?")
                        _esum = str(exc).replace("\n", " ")[:160]
                        # Bounded SCHEMA-REPAIR: a typed-output validation/parse miss (a
                        # required field dropped/null/unparseable by a model that HAS the
                        # evidence) is re-askable. Each retry is an independent sample at a
                        # bumped temperature; the model corrects its own drop (not a
                        # default, not hiding).
                        if _repair_attempt < _max_repairs and _is_repairable_typed_output_error(
                            exc
                        ):
                            hint = _typed_output_repair_hint(exc)
                            if self.kind == "react":
                                retained = _ctx.active_trajectory()
                                has_traj = isinstance(retained, dict) and bool(
                                    retained.get("trajectory")
                                )
                                if has_traj:
                                    # Extract-format miss: re-run ONLY the final extract
                                    # over the retained trajectory, multiple times at
                                    # increasing temperature (cheap; no tool-loop restart).
                                    reextracted = None
                                    for _re_i in range(1, _max_repairs + 1):
                                        _re_temp = _repair_temperature(_base_temp, _re_i)
                                        with dspy.context(
                                            lm=create_lm(
                                                replace(self.config, temperature=_re_temp)
                                            ),
                                            adapter=adapter,
                                        ):
                                            reextracted = _reextract_over_retained_trajectory(
                                                self.program, hint
                                            )
                                        if reextracted is not None:
                                            trace.event(
                                                "SCHEMA-REPAIR",
                                                "%s :: re-extract-only ok (try %d temp %.2f) :: %s",
                                                _eid,
                                                _re_i,
                                                _re_temp,
                                                _esum,
                                            )
                                            break
                                    if reextracted is not None:
                                        result = reextracted
                                        break
                                    # All re-extracts failed -> recover intent / surface;
                                    # do NOT full-re-ask (the tool loop already succeeded,
                                    # restarting it just re-bloats the prompt).
                                    trace.event(
                                        "SCHEMA-REPAIR",
                                        "%s :: re-extract exhausted (%d tries) :: %s",
                                        _eid,
                                        _max_repairs,
                                        _esum,
                                    )
                                else:
                                    # No retained trajectory: the model failed at the FIRST
                                    # react step -- it emitted PROSE instead of the
                                    # next_tool_name/next_tool_args fields and ran NO tools
                                    # (observed: ndp_dataset_discovery wrote "I need to
                                    # execute three tool calls..." as prose). So a full
                                    # re-ask is CHEAP (nothing to restart) and each is an
                                    # INDEPENDENT sample that may format correctly. Re-ask up
                                    # to _max_repairs via the outer loop (not just once);
                                    # token-liveness keeps a slow re-ask alive.
                                    _repair_hint = hint
                                    trace.event(
                                        "SCHEMA-REPAIR",
                                        "%s :: no trajectory -> full re-ask (attempt %d) :: %s",
                                        _eid,
                                        _repair_attempt + 1,
                                        _esum,
                                    )
                                    continue
                            else:
                                # predict/CoT: re-ask the whole (cheap) program at the next
                                # attempt's bumped temperature.
                                _repair_hint = hint
                                trace.event(
                                    "SCHEMA-REPAIR",
                                    "%s :: re-asking (attempt %d) :: %s",
                                    _eid,
                                    _repair_attempt + 1,
                                    _esum,
                                )
                                continue
                        recovered = (
                            _recover_blueprint_react_tool_intent(
                                tools=self.tools,
                                exc=exc,
                            )
                            if self.kind == "react"
                            else None
                        )
                        if recovered is None:
                            raise
                        result = recovered
                        break
            finally:
                # Single-var stack: each token captured the FULL context at its set,
                # so reset MUST unwind in strict reverse-LIFO of the sets
                # (window -> session -> scope -> rows) or an earlier reset would
                # restore a snapshot that predates the later sets. (#714)
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
            answer = str(getattr(result, "answer", "") or "").strip()
            tools_called: list[dict[str, Any]] = []
            if self.kind == "react":
                tools_called = _extract_tools_called_from_trajectory(
                    getattr(result, "trajectory", None)
                )
                if blueprint_tool_rows:
                    tools_called = _merge_tool_call_rows(tools_called, blueprint_tool_rows)
                if not answer:
                    answer = _tool_agent_empty_answer_fallback(getattr(result, "trajectory", None))
            # The typed workflow_state output rides the Prediction's structured
            # ``workflow_state`` field below -- it is NOT serialized into ``answer``
            # text (that polluted the user-facing answer; consumers read the field).
            handoff_rows = _coerce_expert_handoff_rows(getattr(result, "expert_handoffs", None))
            if not answer and not handoff_rows:
                return dspy.Prediction(
                    answer="",
                    selected_expert=self.agent_def.id,
                    routing_rationale=(
                        "Blueprint expert returned an empty answer; CLIO will attempt "
                        "runtime settlement or declared-child handoff repair."
                    ),
                    route_source="agent_blueprint",
                    session_id=session_id,
                    expert_handoffs=[],
                    next_expert=getattr(result, "next_expert", ""),
                    next_task=getattr(result, "next_task", ""),
                    workflow_state=getattr(result, "workflow_state", ""),
                    evidence=getattr(result, "evidence", ""),
                    artifacts=getattr(result, "artifacts", ""),
                    errors=getattr(result, "errors", ""),
                    delegation=getattr(result, "delegation", ""),
                    trajectory=getattr(result, "trajectory", None),
                    reasoning=getattr(result, "reasoning", ""),
                    tools_called=tools_called,
                    error_info=None,
                )
            return dspy.Prediction(
                answer=answer,
                selected_expert=self.agent_def.id,
                routing_rationale=f"Session selected Agent Blueprint expert {self.agent_def.id!r}.",
                route_source="agent_blueprint",
                session_id=session_id,
                expert_handoffs=handoff_rows,
                next_expert=getattr(result, "next_expert", ""),
                next_task=getattr(result, "next_task", ""),
                workflow_state=getattr(result, "workflow_state", ""),
                evidence=getattr(result, "evidence", ""),
                artifacts=getattr(result, "artifacts", ""),
                errors=getattr(result, "errors", ""),
                delegation=getattr(result, "delegation", ""),
                trajectory=getattr(result, "trajectory", None),
                reasoning=getattr(result, "reasoning", ""),
                tools_called=tools_called,
                error_info=None,
            )

    return BlueprintExpertModule(base_agent, agent_def)


def _build_tool_user_agent_module(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Build a DSPy ReAct wrapper for a streamable tool-declaring dynamic agent."""

    import dspy  # noqa: PLC0415

    from clio_agent.config import (  # noqa: PLC0415
        create_chat_adapter,
        create_lm,
    )
    from clio_agent.gact.app import (  # noqa: PLC0415
        _cancelled_error_info,
        _coerce_expert_handoff_rows,
        _extract_tools_called_from_trajectory,
        _tool_agent_empty_answer_fallback,
    )
    from clio_agent.prompts import PromptRegistry  # noqa: PLC0415

    class ToolUserAgentModule(dspy.Module):
        def __init__(self, base_agent: Any, agent_def: "AgentDef") -> None:
            super().__init__()
            self.agent_def = agent_def
            self.config = _dynamic_agent_lm_config(base_agent, agent_def)
            self._provider_config = self.config
            self.tools = _dynamic_agent_tools(base_agent, agent_def)
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
                for part in (runtime_text, agent_prompt, workspace_context, child_context)
                if part
            )
            # Use the retaining ReAct subclass so this path also runs the ARC
            # live-context plane (writes segments + reads its prompt from ARC).
            self.react_agent = _retaining_react_cls()(
                _tool_user_agent_signature(),
                tools=self.tools,
                max_iters=_tool_user_agent_max_iters(agent_def),
            )
            self.answer_synthesizer = self.react_agent.extract.predict

        def forward(
            self,
            question: str,
            session_id: str,
            session_mode: str = "chat",
            session_edit_mode: str = "diff",
            cancel_requested: Any | None = None,
        ) -> Any:
            del session_mode, session_edit_mode
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            # ARC live-context-plane wiring for this tool-user ReAct loop.
            _react_scope_token = _ctx.set_react_scope(str(getattr(self.agent_def, "id", "")))
            _react_session_token = _ctx.set_react_session(session_id)
            _react_window_token = _ctx.set_react_window(_resolve_expert_context_window(self.config))
            try:
                # track_usage installs the tracker so auto-compaction can read each
                # call's exact prompt_tokens.
                with (
                    dspy.track_usage(),
                    dspy.context(
                        lm=create_lm(self.config),
                        adapter=create_chat_adapter(self.config),
                    ),
                ):
                    result = self.react_agent(
                        system_prompt=self.system_prompt,
                        question=question,
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
            answer = str(getattr(result, "answer", "") or "").strip()
            if not answer:
                answer = _tool_agent_empty_answer_fallback(getattr(result, "trajectory", None))
            if not answer:
                raise RuntimeError(f"user agent {self.agent_def.id!r} returned an empty answer")
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
                error_info=None,
            )

    return ToolUserAgentModule(base_agent, agent_def)
