"""Build the running DSPy module of a resolved plugin agent.

An agent's module is built from its pinned resolution
(:mod:`clio_agent.gact.agents.resolution`): the composed prompt, the built-in
tools its plugins claim (:mod:`~clio_agent.gact.agents.native_tool_registry`),
its plugins' Python tools, every tool of its plugins' MCP servers, its plugins'
skills and, when the ``delegation`` plugin claims them, the spawn tools. The
agent's flow runs the program; ``react`` (from ``core``) is the default.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import skill_runtime as _skill_runtime
from clio_agent.gact.agents import toolset_inventory
from clio_agent.gact.agents.always_load_tools import attach_always_load_tools
from clio_agent.gact.agents.composition import _runtime_active_workspace_context
from clio_agent.gact.agents.lm_resolution import (
    _default_profile_spec as _default_profile_spec,
)
from clio_agent.gact.agents.lm_resolution import (
    _dynamic_agent_lm_config as _dynamic_agent_lm_config,
)
from clio_agent.gact.agents.native_tool_registry import (
    AGENT_RUNTIME_TOOLS,
    build_native_tools,
    declared_native_capabilities,
)
from clio_agent.gact.agents.prompt_basis import unlisted_tool_names
from clio_agent.gact.agents.resolution import session_workflow_state_schema
from clio_agent.gact.agents.tool_executor_resolution import (
    resolve_active_base_agent_tool_executor,
)
from clio_agent.gact.agents.tool_recording import (
    recorded_load_skill_tool as _recorded_load_skill_tool,
)
from clio_agent.gact.agents.tool_recording import (
    recorded_spawn_skill_task_tool as _recorded_spawn_skill_task_tool,
)
from clio_agent.gact.agents.tool_recording import (
    recording_tool as _recording_tool,
)
from clio_agent.gact.runtime.context_tokens import _resolve_expert_context_window
from clio_agent.gact.runtime.globals import (
    _emit_semantic_event,
    _llm_provider_payload,
    _TurnCancelled,
    _UnsupportedSessionAgent,
)
from clio_agent.gact.runtime.type_parsing import (
    _parse_field_annotation,
    _structured_output_enabled,
)
from clio_agent.gact.task_declarations import build_spawn_runtime_tools
from clio_agent.lm.session_clients import session_model
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema
    from clio_agent.plugins.resolve import Resolution


def _active_base_agent_tool_executor(base_agent: Any) -> Any:
    """Return the base agent's tool executor for the active session workspace.

    Tools are bound to a concrete executor, so the cwd of its stdio MCP
    subprocesses is fixed at bind time; the per-workspace executor spawns them
    with ``cwd=<workspace root>`` so staged artifacts land in the workspace.
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


def _listed_tools(tool_executor: Any) -> dict[str, Any]:
    return {
        name: tool
        for tool in tool_executor.to_dspy_tools()
        for name in [str(getattr(tool, "name", "") or "")]
        if name
    }


def _gateway_and_namespace_tools(
    tool_executor: Any, gateway_names: list[str], namespaces: list[str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Gateway built-ins by name plus EVERY tool of each plugin MCP namespace.

    A plugin's MCP server contributes all of its tools. A namespace whose tools
    are not listed yet is mounted on demand through the session-readiness
    boundary (#1237); a failed mount is returned as a typed reason per
    namespace for this resolve only.
    """

    from clio_agent.gact.mcp_readiness import mount_namespaces_for_session  # noqa: PLC0415

    available = _listed_tools(tool_executor)
    declared_specs: Mapping[str, Any] = getattr(tool_executor, "_clio_namespace_specs", None) or {}
    failures: dict[str, str] = {}
    needed: dict[str, Any] = {}
    for namespace in namespaces:
        if namespace not in declared_specs:
            failures[namespace] = "mcp_server_not_declared"
            continue
        if not any(name.startswith(f"{namespace}_") for name in available):
            needed[namespace] = declared_specs[namespace]
    for name in gateway_names:
        namespace = name.partition("_")[0]
        if name not in available and namespace in declared_specs:
            needed.setdefault(namespace, declared_specs[namespace])
    if needed:
        mounted, mount_failures = mount_namespaces_for_session(tool_executor, needed, connect=False)
        failures.update(mount_failures)
        if any(mounted.values()):
            available = _listed_tools(tool_executor)
    wanted = set(gateway_names)
    prefixes = tuple(f"{namespace}_" for namespace in namespaces)
    picked = {
        name: tool
        for name, tool in available.items()
        if name in wanted or (prefixes and name.startswith(prefixes))
    }
    for namespace in namespaces:
        if namespace not in failures and not any(
            name.startswith(f"{namespace}_") for name in picked
        ):
            failures[namespace] = "mcp_server_listed_no_tools"
    return picked, failures


def _python_tools(resolution: "Resolution", sources: dict[str, str]) -> list[Any]:
    """The plugins' own Python tools as DSPy tools."""

    from clio_agent.gact.agents.tool_instrumentation import plugin_tool  # noqa: PLC0415
    from clio_agent.plugins.loader import load_attribute  # noqa: PLC0415

    tools = []
    for row in resolution.tools:
        if row["kind"] != "python":
            continue
        spec = load_attribute(row["root"], row["module"], row["attr"])
        tools.append(
            plugin_tool(
                spec.function,
                name=row["name"],
                desc=row["description"] or None,
                read_only=bool(row.get("read_only")),
            )
        )
        toolset_inventory.register_tool_source(sources, row["name"], f"plugin:{row['plugin']}")
    return tools


def _agent_tools(
    base_agent: Any,
    agent_def: "AgentDef",
    resolution: "Resolution",
    sources: dict[str, str],
    *,
    supports_vision: bool,
    supports_pdf: bool,
) -> list[Any]:
    """Every tool the resolved agent's plugins provide, built for this turn."""

    claimed = [row["name"] for row in resolution.tools if row["kind"] == "native"]
    allowed = set(agent_def.tools)
    claimed = [name for name in claimed if name in allowed and name not in AGENT_RUNTIME_TOOLS]
    available, gateway_names, withheld = build_native_tools(
        agent_def,
        claimed,
        sources,
        supports_vision=supports_vision,
        supports_pdf=supports_pdf,
        a2ui_catalogs=bool(resolution.catalogs),
    )
    namespaces = sorted(resolution.mcp_servers) if agent_def.tools else []
    mount_failures: dict[str, str] = {}
    if gateway_names or namespaces:
        try:
            tool_executor = _active_base_agent_tool_executor(base_agent)
        except Exception as exc:  # noqa: BLE001 - preserve the typed agent boundary
            from clio_agent.gact.mcp_readiness import mount_failure_reason  # noqa: PLC0415

            raise _UnsupportedSessionAgent(
                agent_def.id,
                reason="custom_agent_tool_executor_unavailable",
                tools=[*gateway_names, *namespaces],
                mount_failures={"workspace_fleet": mount_failure_reason(exc)},
            ) from exc
        if tool_executor is None or not hasattr(tool_executor, "to_dspy_tools"):
            raise _UnsupportedSessionAgent(
                agent_def.id,
                reason="custom_agent_tool_executor_unavailable",
                tools=[*gateway_names, *namespaces],
            )
        picked, mount_failures = _gateway_and_namespace_tools(
            tool_executor, gateway_names, namespaces
        )
        if not agent_def.parent_id:
            attach_always_load_tools(tool_executor, picked, mount_failures, list(picked))
        mounted = toolset_inventory.mounted_namespace_set(tool_executor)
        for name in picked:
            prefix, sep, bare = name.partition("_")
            source = prefix if (sep and bare and prefix in mounted) else "gateway"
            toolset_inventory.register_tool_source(sources, name, source)
        available.update(picked)
    missing = [name for name in gateway_names if name not in available]
    unavailable = {**withheld, **dict.fromkeys(missing, "gateway_tool_unlisted")}
    unavailable.update({f"{ns}_*": reason for ns, reason in mount_failures.items()})
    if unavailable:
        toolset_inventory.record_tools_unavailable_degraded(
            _ctx.active_app(), agent_def.id, sorted(unavailable), mount_failures=unavailable
        )
    tools = [_recording_tool(tool) for tool in available.values()]
    tools.extend(_recording_tool(tool) for tool in _python_tools(resolution, sources))
    return tools


def _tool_user_agent_max_iters(agent_def: "AgentDef", *, declared_children: int = 0) -> int:
    """The react loop's iteration budget for this agent.

    #1226 D1b: UNLIMITED (``0``) default. A cap is only an explicit opt-in
    (a defined subagent's ``max_iters``).
    """

    del declared_children
    raw = (agent_def.parameters or {}).get("max_iters", 0)
    try:
        max_iters = int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("agent parameter 'max_iters' must be an integer") from exc
    if max_iters < 0:
        raise ValueError("user agent parameter 'max_iters' must be zero (unlimited) or positive")
    return max_iters


def _injected_workflow_state_field_type(schema: "WorkflowStateSchema") -> Any:
    """Annotation for the auto-injected ``workflow_state`` output (Consumer A, #648).

    The type is built FROM THE PLUGIN SCHEMA, not hardcoded:

    * GENERIC schema (no declared sections) -> ``dict[str, Any]`` byte-identically,
      preserving the historical free-dict contract for domain-free agents.
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


def _agent_signature(
    agent_def: "AgentDef",
    *,
    app: Any = None,
    include_images: bool = False,
    include_files: bool = False,
) -> Any:
    """Build the DSPy Signature for an agent: system prompt and question in, answer out.

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
    # An agent definition that declares its own inputs (e.g. just `question`) REPLACES
    # the defaults, which silently dropped the `system_prompt` input -- so the agent's
    # built system prompt (composed prompt + workspace context, assembled into
    # runtime_system_prompt) was never passed to the model at all; it
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
    # when they sensibly omitted an empty one -- so we no longer auto-inject them. (An
    # agent definition that genuinely needs one can still declare it in its `outputs:`.)
    # Resolve the session's active workflow_state schema once. Consumer A (#648): the injected
    # workflow_state field is TYPED FROM THE PLUGIN SCHEMA -- a GENERIC (no declared
    # sections) schema keeps the historical free ``dict[str, Any]`` byte-identically,
    # while a plugin that declares its vocabulary gets a nested pydantic model whose
    # per-section ``status`` is a real ``Optional[Literal[...]]`` (undeclared keys and
    # undeclared sections still validate via ``extra="allow"`` at both levels).
    _route_app = app if app is not None else _ctx.active_app()
    _route_sid = _ctx.active_session_id()
    # No try/except: the resolver already returns GENERIC for app-less/session-less
    # callers, so any exception here is a real defect that must propagate loudly
    # (matching every other call site of the resolver -- turn.py and the two
    # delegate/seed sites below). Swallowing it would silently downgrade the
    # injected annotation with no recorded reason (no-silent-fallback ground rule).
    _workflow_state_schema = session_workflow_state_schema(_route_app, _route_sid)
    _workflow_state_field_type = _injected_workflow_state_field_type(_workflow_state_schema)
    _structured_field_specs: dict[str, tuple[str, Any]] = {
        "workflow_state": (
            "Typed semantic workflow state (a JSON object) handed back to the delegating agent.",
            _workflow_state_field_type,
        ),
    }
    _declared = {field for field, _, _ in outputs}
    for name, (desc, field_type) in _structured_field_specs.items():
        if _structured_output_enabled(structured.get(name, False)) and name not in _declared:
            outputs.append((name, desc, field_type))

    # #948 S4: an agent with defined subagents routes by CALLING
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
    # never fired. The typed field rides the ``submit`` tool's JSON schema, so the
    # model is asked for exactly these keys.
    if trace.HF_ON:
        trace.hot(
            "SIG-BUILD",
            "%s :: workflow_state type=%s",
            getattr(agent_def, "id", "?"),
            next((str(t) for n, d, t in outputs if n == "workflow_state"), "<none>"),
        )

    namespace: dict[str, Any] = {"__doc__": f"DSPy signature for agent {agent_def.id}."}
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
        f"{agent_def.id.title().replace('-', '').replace('_', '')}AgentSignature",
        (dspy.Signature,),
        namespace,
    )


def _emit_agent_llm_failure(agent_def: "AgentDef", kind: str, exc: BaseException) -> None:
    """Best-effort failure event carrying the exception in the durable trace."""

    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        return
    payload: dict[str, Any] = {
        # SSE/UI gets a summary; canonical trace keeps the uncapped exception.
        "error": str(exc).replace("\n", " ")[:2000],
        "error_full": str(exc),
        "error_type": type(exc).__name__,
    }
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


def _plan_editor() -> dict[str, Any] | None:
    """The active session's ``plan_editor`` grant: set while it edits a running plan."""

    app = _ctx.active_app()
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(_ctx.active_session_id()) if sessions is not None else None
    editor = (getattr(session, "metadata", None) or {}).get("plan_editor")
    return dict(editor) if isinstance(editor, Mapping) else None


def build_agent_module(base_agent: Any, agent_def: "AgentDef", resolution: "Resolution") -> Any:
    """Compile a resolved plugin agent into its running DSPy module."""

    import dspy  # noqa: PLC0415

    from clio_agent.gact.agents import module_variants as _module_variants  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import ClioReAct  # noqa: PLC0415
    from clio_agent.gact.app import (  # noqa: PLC0415
        _cancelled_error_info,
        _coerce_expert_handoff_rows,
        _merge_tool_call_rows,
    )
    from clio_agent.providers.credentials import CredentialResolver  # noqa: PLC0415

    class PluginAgentModule(dspy.Module):
        def __init__(
            self, base_agent: Any, agent_def: "AgentDef", resolution: "Resolution"
        ) -> None:
            super().__init__()
            self.agent_def = agent_def
            self.resolution = resolution
            self.kind = "react" if agent_def.module.get("kind", "react") == "react" else "predict"
            # Per-agent provider identity as data; the credential is resolved fresh
            # per forward() via ``self._resolved_spec.materialize``.
            self._resolved_spec = _dynamic_agent_lm_config(base_agent, agent_def)
            self._cred_resolver = CredentialResolver()
            self.config = self._resolved_spec.materialize(self._cred_resolver)
            self._provider_config = self.config
            self.signature = _agent_signature(agent_def, include_images=True, include_files=True)
            self.tools: list[Any] = []
            skill_rt = _skill_runtime.skill_runtime_for_agent(
                _ctx.active_app(), agent_def, session_id=_ctx.active_session_id()
            )
            if self.kind == "react":
                sources = cast(dict[str, str], {})
                tools = _agent_tools(
                    base_agent,
                    agent_def,
                    resolution,
                    sources,
                    **declared_native_capabilities(self.config),
                )
                claimed = {row["name"] for row in resolution.tools if row["kind"] == "native"}
                if claimed & {"spawn_agent_task", "spawn_agents_parallel"}:
                    spawn_tools = [
                        tool
                        for tool in build_spawn_runtime_tools(
                            base_agent, agent_def, resolution=resolution
                        )
                        if tool.name in claimed
                    ]
                    toolset_inventory.register_tool_sources(sources, spawn_tools, "spawn-runtime")
                    tools.extend(spawn_tools)
                if skill_rt.resolved and "load_skill" in claimed:
                    skill_tool = _recorded_load_skill_tool(agent_def, skill_rt)
                    toolset_inventory.register_tool_sources(sources, [skill_tool], "native")
                    tools.append(skill_tool)
                    if (
                        "spawn_skill_task" in claimed
                        and _skill_runtime.skill_runtime_spawns_subagents(skill_rt)
                    ):
                        spawn_skill = _recorded_spawn_skill_task_tool(agent_def, skill_rt)
                        toolset_inventory.register_tool_sources(
                            sources, [spawn_skill], "spawn-runtime"
                        )
                        tools.append(spawn_skill)
                if "draft_alternatives" in claimed:
                    from clio_agent.gact.agents.variant_drafts import (  # noqa: PLC0415
                        build_draft_alternatives_tool,
                    )

                    draft_tool = build_draft_alternatives_tool()
                    toolset_inventory.register_tool_sources(sources, [draft_tool], "native")
                    tools.append(draft_tool)
                editor = _plan_editor()
                if editor is not None:
                    from clio_agent.gact.flow_tools import (  # noqa: PLC0415
                        build_plan_editor_tools,
                    )

                    editor_tools = build_plan_editor_tools(editor)
                    toolset_inventory.register_tool_sources(sources, editor_tools, "native")
                    tools.extend(editor_tools)
                from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
                    instrument_tools,
                )

                # THE assembly seam: every tool is observed by definition.
                tools = instrument_tools(tools)
                self.tools = tools
                toolset_inventory.emit_agent_toolset_recorded(agent_def, tools, sources)
                self.program = ClioReAct(
                    self.signature, tools=tools, max_iters=_tool_user_agent_max_iters(agent_def)
                )
            else:
                # An agent stripped to zero tools (tool_allowlist == []) answers directly.
                self.program = ClioReAct(self.signature, tools=[])
            object.__setattr__(self.program, "_clio_expert_id", agent_def.id)
            object.__setattr__(
                self.program,
                "_clio_unlisted",
                unlisted_tool_names(agent_def.metadata, getattr(self.program, "tools", {})),
            )
            # A spawn-requested BestOfN / Refine strategy wraps the program.
            strategy = _module_variants.session_strategy()
            if strategy is not None:
                self.program = _module_variants.wrap_strategy(self.program, agent_def.id, strategy)
            active_app = _ctx.active_app()
            workspace_context = (
                _runtime_active_workspace_context(active_app, session_id=_ctx.active_session_id())
                if active_app is not None and self.kind == "react"
                else ""
            )
            # Progressive disclosure (#919): react agents get skill metadata and
            # load_skill; a tool-less agent gets the bodies.
            skills_context = (
                skill_rt.prompt_block if self.kind == "react" else skill_rt.bodies_block
            )
            self.system_prompt = "\n\n".join(
                part
                for part in (agent_def.system_prompt.strip(), workspace_context, skills_context)
                if part
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
            _ = (session_mode, session_edit_mode)  # surfaced in turn.py enrichment
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(session_id, execution_cancellation="cooperative")
                )
            kwargs: dict[str, Any] = {"question": question}
            if "system_prompt" in self.signature.input_fields:
                kwargs["system_prompt"] = self.system_prompt
            if "images" in self.signature.input_fields:
                kwargs["images"] = list(images or [])
            if "files" in self.signature.input_fields:
                kwargs["files"] = list(files or [])
            tool_rows: list[dict[str, Any]] = []
            tool_rows_token = _ctx.set_agent_tool_rows(tool_rows) if self.kind == "react" else None
            agent_id = str(getattr(self.agent_def, "id", "") or "")
            # ARC live-context-plane wiring (scope, session, window).
            scope_token = _ctx.set_react_scope(agent_id, self.kind)
            session_token = _ctx.set_react_session(_ctx.active_session_id())
            window_token = _ctx.set_react_window(_resolve_expert_context_window(self.config))
            structured = (
                self.agent_def.structured_outputs
                if isinstance(self.agent_def.structured_outputs, Mapping)
                else {}
            )
            # A typed ``workflow_state`` extract keeps the answer behind *show more*.
            answer_visible = not _structured_output_enabled(
                structured.get("workflow_state") or False
            )
            visible_token = _ctx.set_visible_answer_stream(answer_visible)
            try:
                forward_config = self._resolved_spec.materialize(self._cred_resolver)
                try:
                    with (
                        session_model(forward_config, self.agent_def.id, session_id) as lm,
                        dspy.track_usage(),
                        dspy.context(lm=lm),
                    ):
                        result = self._run_flow(kwargs)
                except Exception as exc:
                    _emit_agent_llm_failure(self.agent_def, self.kind, exc)
                    raise
            finally:
                _ctx.reset(visible_token)
                _ctx.reset(window_token)
                _ctx.reset(session_token)
                _ctx.reset(scope_token)
                if tool_rows_token is not None:
                    _ctx.reset(tool_rows_token)
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(session_id, execution_cancellation="cooperative")
                )
            answer = str(getattr(result, "answer", "") or "")
            tools_called = _merge_tool_call_rows([], tool_rows) if tool_rows else []
            handoff_rows = _coerce_expert_handoff_rows(getattr(result, "expert_handoffs", None))
            flow_id = str((self.agent_def.metadata.get("flow") or {}).get("id") or "react")
            return dspy.Prediction(
                answer=answer,
                selected_expert=self.agent_def.id,
                routing_rationale=f"Session runs agent {self.agent_def.metadata.get('agent_ref')!r}.",
                route_source=f"flow:{flow_id}",
                session_id=session_id,
                expert_handoffs=handoff_rows,
                workflow_state=getattr(result, "workflow_state", ""),
                evidence=getattr(result, "evidence", ""),
                errors=getattr(result, "errors", ""),
                delegation=getattr(result, "delegation", ""),
                reasoning=getattr(result, "reasoning", ""),
                tools_called=tools_called,
                assignment_disposition=getattr(result, "assignment_disposition", None),
                clio_context_boundary=getattr(result, "clio_context_boundary", None),
                termination_reason=getattr(result, "termination_reason", ""),
                variant_selection=getattr(result, "variant_selection", None),
                error_info=getattr(result, "error_info", None),
            )

        def _run_flow(self, kwargs: dict[str, Any]) -> Any:
            """Run the agent's selected flow (``react`` by default) on this turn."""

            from clio_agent.flows.runner import accept_flow_input, run_agent_flow  # noqa: PLC0415

            accept_flow_input(self, str(kwargs["question"]), _ctx.active_session_id())
            return run_agent_flow(self, kwargs)

    return PluginAgentModule(base_agent, agent_def, resolution)
