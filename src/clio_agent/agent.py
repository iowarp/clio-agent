"""
ClioAgent - Main Agent Module

Agent-loop architecture over registered tools.

Architecture:
    User Query -> Planner action
        -> tool call -> observation -> Planner action
        -> answer from observations

Usage:
    >>> from clio_agent import ClioAgent
    >>> from clio_agent.config import setup_dspy
    >>>
    >>> lm = setup_dspy()
    >>> agent = ClioAgent()
    >>> result = agent(question="How do I optimize HDF5 files?")
    >>> print(result.answer)
    >>> print(result.selected_expert)
"""

import contextvars
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Literal

import dspy

from clio_agent import conf
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.retrieval import ContextRetriever
from clio_agent.arc.schema import (
    Conversation,
    Invocation,
    Message,
    NanoagentSpawn,
    RoutingDecision,
)
from clio_agent.arc.storage import make_arc_store
from clio_agent.config import (
    LMProviderConfig,
    create_chat_adapter,
    create_lm,
    create_planner_lm,
    has_explicit_model_override,
    list_lm_studio_models,
    load_config_from_env,
    select_models_for_agents,
)
from clio_agent.errors import (
    CancellationError,
    ClioError,
    ExpertError,
    ProviderError,
    RoutingError,
)
from clio_agent.harness import (
    SPECIAL_ROUTE_TARGETS,
    RouteDecision,
    RunTrace,
    compact_tool_result,
    extract_file_paths,
    normalize_tool_error,
    normalize_tool_result,
    tool_result_ok,
)
from clio_agent.optimizer.instrumentation import _extract_output
from clio_agent.providers.stateful_common import stateful_scope as _stateful_scope
from clio_agent.registry.registry import AgentCapability, AgentRegistry

# Generic path-detection allowlist: file suffixes recognized when extracting
# candidate file paths from free text. Re-exported from the shared vocabulary
# module (single source of truth — see clio_agent.scientific_suffixes).
from clio_agent.scientific_suffixes import SCIENTIFIC_FILE_SUFFIXES
from clio_agent.signatures.main_agent_sig import (
    AgentActionSignature,
    AgentAnswerSignature,
    ChatAgentSignature,
)
from clio_agent.tools.catalog import (
    set_active_catalog,
    tool_owner,
    tool_tags,
    tool_visible_to,
)
from clio_agent.tools.execution import (
    create_sync_tool_executor,
    get_active_tool_workspace_root,
)
from clio_agent.tools.gateway import (
    build_gateway,
    build_tool_catalog,
    list_tool_definitions,
    namespace_proxies,
)
from clio_agent.tools.mcp_config import load_mcp_servers

logger = logging.getLogger(__name__)


def _clio_agent_version() -> str:
    """Return the installed clio-agent package version.

    Stamped into ARC conversation metadata so persisted records carry the
    build that wrote them. Falls back to the in-tree ``__version__`` when the
    distribution metadata is unavailable (e.g. a non-installed source tree).
    """

    from importlib import metadata  # noqa: PLC0415 - local to keep import list lean

    try:
        return metadata.version("clio-agent")
    except metadata.PackageNotFoundError:
        import clio_agent  # noqa: PLC0415

        return str(getattr(clio_agent, "__version__", "0.0.0"))


PLANNER_HIDDEN_TOOL_NAMES = {"fs_read_file", "fs_apply_edit_write"}

# Action kinds the agent loop can execute. Enum validation happens at the
# parse layer (_parse_action_json) as the sanctioned format-only barrier.
SUPPORTED_PLANNER_ACTION_KINDS = frozenset({"tool", "answer", "none"})


class UnsupportedPlannerActionError(ValueError):
    """Planner returned well-formed JSON whose action kind has no executor.

    Raised by :meth:`ClioAgent._parse_action_json` so the agent loop can
    surface the rejected action back to the planner as a structured
    ``planner_error`` observation and re-ask. The model stays the decider;
    CLIO does not reroute, scrub, or fabricate a decision on its behalf.
    """

    def __init__(self, action: dict[str, Any]) -> None:
        kind = str(action.get("action", "")).strip().lower()
        super().__init__(
            f"Planner returned unsupported action {kind!r}. "
            f"Supported actions: {', '.join(sorted(SUPPORTED_PLANNER_ACTION_KINDS))}."
        )
        self.kind = kind
        self.action = action


_ROUTING_MODE_OVERRIDE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "clio_routing_mode_override",
    default="",
)
_CANCELLATION_CHECKER: contextvars.ContextVar[Callable[[], bool] | None] = contextvars.ContextVar(
    "clio_cancellation_checker", default=None
)


@contextmanager
def routing_mode_override(mode: str) -> Iterator[None]:
    """Scope a GACT routing override to the current turn context."""

    token = _ROUTING_MODE_OVERRIDE.set(str(mode or "auto"))
    try:
        yield
    finally:
        _ROUTING_MODE_OVERRIDE.reset(token)


@contextmanager
def cancellation_checker(checker: Callable[[], bool] | None) -> Iterator[None]:
    """Scope a cooperative cancellation checker to the current agent turn."""

    token = _CANCELLATION_CHECKER.set(checker)
    try:
        yield
    finally:
        _CANCELLATION_CHECKER.reset(token)


def cancellation_requested() -> bool:
    """Return whether the active cooperative cancellation checker is set."""

    checker = _CANCELLATION_CHECKER.get()
    return bool(checker is not None and checker())


DEFAULT_AGENT_MAX_STEPS = 8
ERROR_RECOVERY_ACTIONS = ("retry", "reconfigure_provider", "exit")


class ClioAgent(dspy.Module):
    """CLIO Agent with a planner loop over registered tools.

    Architecture:
        User Query -> Planner action
            -> tool call -> observation -> next planner action
            -> answer from observations or conversation

    Attributes:
        action_planner: DSPy Predict module with AgentActionSignature
        chat_agent: DSPy Predict module with ChatAgentSignature
        arc: ARC Memory instance
        context_retriever: Context retrieval module
        _tool_definitions: preloaded tool defs from the boot listing pass
            (#932); None -> executors fall back to eager list_tools
        registry: Agent registry for discovery

    Example:
        >>> agent = ClioAgent()
        >>> result = agent(question="Optimize my HDF5 file", session_id="session-123")
        >>> print(result.answer)
        >>> print(result.selected_expert)  # "chat", "utility", or tool-owner metadata
    """

    # Class default so partially-constructed instances (test stubs) resolve it.
    _tool_definitions: dict[str, Any] | None = None

    def __init__(
        self,
        verbose: bool = False,
        data_dir: str = ".clio/agent",
        arc: ARCMemory | None = None,
        provider_config: LMProviderConfig | None = None,
    ):
        """Initialize ClioAgent with planner, chat, tool execution, and runtime storage.

        Args:
            verbose: If True, print reasoning and decisions
            data_dir: Base directory for ClioAgent data storage
            arc: An EXISTING ARCMemory to reuse. ARC is a per-clio-agent keystone:
                exactly ONE per process (one ARC per clio-agent, the gact server owns
                its lifecycle). When the LM bind rebuilds the agent it MUST inject the
                same ARC here so ``app.state.arc`` stays the SAME object across binds —
                otherwise a fresh ARC's empty ``_events`` strands every event the prior
                ARC already recorded onto the shared durable trace (the trace ⊋ ARC
                split). ``None`` mints a fresh ARC (the standalone CLI / test path that
                owns no server-level ARC).
            provider_config: The default-profile provider config the agent binds its
                ``_main_lm`` / ``_planner_lm`` / ``_dspy_adapter`` from. The gact
                server supplies the config resolved off its authoritative
                ``ProviderProfileStore`` default (design §9 step 9), so the main agent
                and the store agree on ONE identity rather than each reading the
                environment independently (the dropped boot env-handoff). ``None``
                reads :func:`load_config_from_env` directly — the standalone CLI / test
                baseline, byte-identical to before.
        """
        super().__init__()
        self.verbose = verbose

        # ARC Memory: reuse the injected one (the gact server owns the single per-process
        # ARC and re-injects it on every bind) or mint one. The persistence backend comes
        # from the factory: clio-core by default (the gold-standard, in-process tiered
        # store), LocalFSStore via CLIO_ARC_STORE=local. Falls back to LocalFS if the clio-core
        # binding/runtime is unavailable.
        self.arc = (
            arc
            if arc is not None
            else ARCMemory(
                data_dir=f"{data_dir}/arc",
                cache_capacity=1000,
                store=make_arc_store(data_dir=f"{data_dir}/arc"),
            )
        )
        self.context_retriever = ContextRetriever(self.arc)

        # Initialize Agent Registry (for discovery, not routing)
        self.registry = AgentRegistry()

        # Provider identity: bind the injected default-profile config when the gact
        # server supplies one (its ``ProviderProfileStore`` default is the
        # authoritative source — design §9 step 9), so the main agent and the store
        # share ONE identity instead of each reading the environment independently.
        # ``None`` reads the environment directly — the standalone CLI / test
        # baseline, byte-identical to before.
        self._provider_config = (
            provider_config if provider_config is not None else load_config_from_env()
        )

        if self._provider_config.provider == "lm_studio" and not has_explicit_model_override():
            # LM Studio without an explicit model pin: discover loaded models
            # from the configured API base and use the same selected model for
            # planning and the global DSPy runtime.
            available_models = list_lm_studio_models(base_url=self._provider_config.api_base)
            if self.verbose:
                main_model, expert_model = select_models_for_agents(available_models)
            else:
                import contextlib
                import io

                with contextlib.redirect_stdout(io.StringIO()):
                    main_model, expert_model = select_models_for_agents(available_models)
            self._provider_config.model = main_model
        else:
            main_model = self._provider_config.model
            expert_model = self._provider_config.model

        if self.verbose:
            print(f"[ClioAgent] Provider: {self._provider_config.provider}")
            print(f"[ClioAgent] Main/Planner model: {main_model}")
            print(f"[ClioAgent] Expert model: {expert_model}")

        # Planner: a model-chosen action loop over live capabilities.
        self.rebind_lms(self._provider_config)
        self.action_planner = dspy.Predict(AgentActionSignature)
        self.answer_synthesizer = dspy.Predict(AgentAnswerSignature)

        # Chat Agent: Predict for conversational responses. This keeps the
        # structured output surface smaller than ChainOfThought, which is more
        # reliable with local OpenAI-compatible backends.
        self.chat_agent = dspy.Predict(ChatAgentSignature)

        # Shared MCP executor: one explicit sync boundary for CLI/API thread calls.
        # The tool gateway is built from the universal in-process built-ins
        # (fs/shell) PLUS the declared MCP servers for the discovered blueprints
        # and user/workspace config. Declared MCPs are the only source of domain
        # tools; the catalog (ownership/visibility) is derived from the connected
        # namespaces merged with the static built-in entries.
        #
        # Per active workspace: stdio MCP subprocesses are spawned with
        # ``cwd=<workspace root>`` so every stdio tool writes into the workspace
        # by default; http MCPs stay shared. The default (no-cwd) gateway and the
        # process-global tool catalog are built once here (the catalog/tool-set is
        # identical across workspaces). The tool *executor* is then resolved per
        # active workspace via ``_active_tool_executor``: each workspace root keys
        # a lazily built executor over a gateway built with that cwd, so each
        # workspace spawns its stdio MCPs at most once. No active workspace falls
        # back to this default executor (current behavior).
        self._tool_gateway = self._build_tool_gateway(set_catalog=True)
        self.tool_executor = create_sync_tool_executor(
            self._tool_gateway,
            preloaded_tools=self._tool_definitions,
            namespace_servers=namespace_proxies(self._tool_gateway),
        )
        # Cache of workspace root -> sync tool executor (lazy, one per workspace).
        self._workspace_tool_executors: dict[str, Any] = {}

        # Core no longer installs domain experts into the Python registry.
        # Default and baseline agents are blueprint programs loaded through
        # the pinned registry bootstrap path.
        self.registry.register_agent(
            "utility",
            self,
            AgentCapability(
                keywords=["shell", "bash", "terminal", "command", "time", "date", "environment"],
                description=(
                    "Local utility command expert. Exposes the permission-gated "
                    "shell_bash tool for simple local diagnostics such as current time, "
                    "plus workspace edit proposal tools."
                ),
                tools=["shell_bash", "fs_propose_edit"],
                specialization="utility",
            ),
        )

        if self.verbose:
            print(f"[ClioAgent] Registered {self.registry.get_agent_count()} runtime agents")
            print(f"[ClioAgent] ARC Memory initialized at {data_dir}/arc")

    def rebind_lms(self, provider_config: LMProviderConfig) -> None:
        """(Re)build the LM-dependent surface from a provider config.

        The single writer for ``_provider_config`` / ``_main_lm`` / ``_planner_lm`` /
        ``_dspy_adapter``. Used by ``__init__`` and by the gact LM-bind hot-swap, so the
        four fields are always rebuilt together (no partial/torn LM surface).
        """

        self._provider_config = provider_config
        self._main_lm = create_lm(provider_config)
        self._planner_lm = create_planner_lm(provider_config)
        self._dspy_adapter = create_chat_adapter(provider_config)

    def _discover_pack_servers(self) -> dict[str, dict[str, Any]]:
        """Return declared ``mcp_servers`` per discovered blueprint id.

        Reads each blueprint's ``AGENT.md`` frontmatter ``mcp_servers`` map so
        the active pack's declared MCP servers become available tools. Discovery
        failures degrade to "no pack servers" (pure reasoning / built-ins only).
        """

        from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
            discover_agent_blueprints,
        )

        pack_servers: dict[str, dict[str, Any]] = {}
        try:
            blueprints = discover_agent_blueprints()
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            if self.verbose:
                print(f"[ClioAgent] blueprint discovery failed: {exc}")
            return pack_servers
        for blueprint in blueprints:
            servers = blueprint.metadata.get("mcp_servers")
            if isinstance(servers, Mapping) and servers:
                pack_servers[blueprint.id] = {str(k): v for k, v in servers.items()}
        return pack_servers

    def _build_tool_gateway(self, *, cwd: str | None = None, set_catalog: bool = False) -> Any:
        """Build the tool gateway from built-ins + declared MCP servers.

        Merges declared MCP servers across pack ``AGENT.md`` frontmatter and
        user/workspace ``mcp.yaml`` (``load_mcp_servers``), proxy-mounts them next
        to the in-process built-ins (``build_gateway``), then installs the derived
        tool catalog (``build_tool_catalog``) so ownership/visibility for declared
        tools comes from connected namespaces + each expert's ``tools:`` list.

        Args:
            cwd: Working directory for stdio MCP subprocesses (per active
                workspace). Http MCPs stay shared and ignore it. ``None`` keeps
                the process cwd (the default gateway).
            set_catalog: Whether to derive and install the process-global tool
                catalog from this gateway. The catalog/tool-set is identical
                across workspaces, so only the default gateway sets it; per-
                workspace gateways reuse the already-installed catalog.
        """

        pack_servers = self._discover_pack_servers()
        specs = load_mcp_servers(pack_servers=pack_servers)
        tool_gateway = build_gateway(specs, cwd=cwd)
        if not set_catalog:
            return tool_gateway
        experts = self._discover_pack_experts()
        try:
            # ONE transient listing pass (#932/#702): the fleet spawns once,
            # lists, and is reaped; the definitions feed BOTH the catalog and
            # every executor's preloaded_tools so no executor ever re-lists
            # (executor start stops fanning the fleet; servers spawn lazily
            # per namespace on first call).
            self._tool_definitions = list_tool_definitions(tool_gateway)
            catalog = build_tool_catalog(
                tool_gateway, experts=experts, tools=list(self._tool_definitions.values())
            )
            set_active_catalog(catalog)
        except Exception as exc:  # noqa: BLE001 - degrade to static built-ins
            # LOUD degrade (no-silent-fallback): losing the preloaded
            # definitions flips every executor back to eager list_tools — the
            # resident-fleet memory behavior #932 exists to kill. The reason
            # must reach the trace, not just a verbose print.
            from clio_agent.runtime import trace  # noqa: PLC0415

            trace.event(
                "TOOLS",
                "tool_preload_failed reason=%s — catalog degraded to static "
                "built-ins; executors fall back to eager list_tools (#932)",
                exc,
            )
            if self.verbose:
                print(f"[ClioAgent] tool catalog derivation failed: {exc}")
            self._tool_definitions = None
            set_active_catalog(None)
        return tool_gateway

    def _active_tool_executor(self) -> Any:
        """Resolve the tool executor for the active session workspace.

        Reads the active workspace root from the tool-execution contextvar. With
        no active workspace, returns the default (no-cwd) executor (current
        behavior). Otherwise returns a per-workspace executor over a gateway whose
        stdio MCP subprocesses are spawned with ``cwd=<workspace root>`` (http MCPs
        stay shared). The executor is cached per root, so each workspace spawns its
        stdio MCPs at most once (lazy, on first tool use for that workspace).
        """

        root = get_active_tool_workspace_root().strip()
        if not root:
            return self.tool_executor
        executor = self._workspace_tool_executors.get(root)
        if executor is None:
            gateway = self._build_tool_gateway(cwd=root)
            executor = create_sync_tool_executor(
                gateway,
                preloaded_tools=self._tool_definitions,
                namespace_servers=namespace_proxies(gateway),
            )
            self._workspace_tool_executors[root] = executor
        return executor

    def _discover_pack_experts(self) -> list[Any]:
        """Return loaded pack experts (for declared-tool visibility derivation)."""

        from clio_agent.gact.agent_blueprints import load_agent_blueprints  # noqa: PLC0415

        try:
            return list(load_agent_blueprints())
        except Exception as exc:  # noqa: BLE001 - best-effort
            if self.verbose:
                print(f"[ClioAgent] expert discovery failed: {exc}")
            return []

    def forward(
        self,
        question: str,
        session_id: str = "default",
        *,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
        images: list[Any] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dspy.Prediction:
        """Process a question through the CLIO agent loop.

        Flow: retrieve session/file context from ARC → ask the planner for the next
        action → execute tools and append observations → answer → store in ARC.

        Args:
            question: User's question or request
            session_id: Session identifier for conversation tracking
            session_mode: GACT session mode. Currently recorded at the
                boundary; write enforcement happens in the GACT layer.
            session_edit_mode: GACT edit shaping mode. File-diff shaping
                happens in the GACT layer.
            cancel_requested: Optional cooperative cancellation checker
                supplied by service frontends such as GACT.

        Returns:
            dspy.Prediction with answer, selected_expert, session_id,
            duration_ms, arc_stats
        """
        if cancel_requested is not None:
            with cancellation_checker(cancel_requested):
                return self.forward(
                    question,
                    session_id=session_id,
                    session_mode=session_mode,
                    session_edit_mode=session_edit_mode,
                    images=images,
                )

        start_time = time.time()

        # Step 1: Retrieve context from ARC Memory. The gact turn path prepends
        # the full transcript as THE conversation channel, so the compiled
        # context deliberately omits conversation/routing turns (#771).
        session_context = self._get_session_context(
            question, session_id, tool_scope="chat", include_conversation=False
        )
        active_file = self._resolve_session_file_reference(question, session_id)
        file_context = self._get_file_context(session_id, active_file)
        routing_mode = self._effective_routing_mode()

        route = RouteDecision(
            target="chat",
            source="dspy",
            reason="Agent planner started from live CLIO capabilities.",
            confidence=0.0,
        )
        trace = RunTrace(route=route)

        if self.verbose:
            print(f"[Planner] {question[:50]}...")

        success = False
        error_msg = None
        selected = "chat"
        answer = ""
        expert_result = None
        error_info = None

        try:
            # Bind ONE per-forward stateful scope around the Tier-1 planner loop (#891,
            # like the V2 expert ``forward``) so append-only sends are delta-eligible.
            with _stateful_scope():
                selected, answer, expert_result, error_info, route = self._run_agent_loop(
                    question=question,
                    session_context=session_context,
                    file_context=file_context,
                    images=images,
                    trace=trace,
                    routing_mode=routing_mode,
                )
            trace.route = route
            success = True
        except Exception as e:  # noqa: BLE001 - routing failure recorded on the trace (success=False + inferred expert)
            success = False
            inferred_selected = self._selected_expert_from_trace(trace)
            if selected == "chat" and inferred_selected != "chat":
                selected = inferred_selected
                route = RouteDecision(
                    target=selected,
                    source=route.source,
                    reason=(
                        f"{route.reason} Selected expert inferred from completed "
                        "tool provenance after the turn failed."
                    ).strip(),
                    confidence=route.confidence,
                    capabilities=route.capabilities,
                )
            if isinstance(e, RoutingError):
                error_info = self._with_recovery_actions(e.to_dict())
                answer = ""
            elif isinstance(e, ClioError):
                error_info = self._with_recovery_actions(e.to_dict())
                answer = ""
            else:
                agent_err = ExpertError(
                    message="CLIO could not complete the agent loop for this request.",
                    details=self._recovery_details(
                        selected=selected,
                        original_error=str(e),
                    ),
                )
                error_info = agent_err.to_dict()
                answer = ""
            error_msg = str(e)
            if self.verbose:
                print(f"[ClioAgent] Agent loop error: {e}")

        # Step 4b: Store tier-2 expert invocation for optimizer training data
        expert_duration_ms = (time.time() - start_time) * 1000
        nanoagents_spawned = self._extract_nanoagents_spawned(expert_result)
        if selected not in SPECIAL_ROUTE_TARGETS:
            self._store_expert_invocation(
                question=self._question_with_session_file(question, active_file),
                file_context=file_context,
                selected=selected,
                session_id=session_id,
                expert_result=expert_result,
                success=success,
                error_msg=error_msg,
                duration_ms=expert_duration_ms,
                trace=trace,
            )

        # Step 5: Store conversation + routing decision + metrics in ARC
        # Conversation must be stored first so routing decision can append to it
        duration_ms = (time.time() - start_time) * 1000
        self._store_conversation(question, answer, session_id)
        self._store_routing_decision(question, route, session_id)
        self._store_metrics(
            question,
            session_id,
            selected,
            duration_ms,
            success,
            error_msg,
            trace,
            nanoagents_spawned=nanoagents_spawned,
        )

        return dspy.Prediction(
            answer=answer,
            selected_expert=selected,
            tools_called=[tool.to_arc_tool_call() for tool in trace.tools],
            expert_handoffs=[handoff.to_dict() for handoff in trace.expert_handoffs],
            file_diffs=self._file_diffs_from_trace(
                trace,
                edit_mode=session_edit_mode,
            ),
            route_source=route.source,
            route_reason=route.reason,
            session_id=session_id,
            duration_ms=duration_ms,
            arc_stats=self.arc.get_cache_stats(),
            nanoagents_spawned=nanoagents_spawned,
            error_info=error_info,
        )

    def _run_agent_loop(
        self,
        *,
        question: str,
        session_context: str,
        file_context: str,
        trace: RunTrace,
        images: list[Any] | None = None,
        routing_mode: str = "auto",
    ) -> tuple[str, str, Any, dict[str, Any] | None, RouteDecision]:
        """Run the planner/executor loop for one user request."""
        self._raise_if_cancelled("agent_loop_start")
        image_inputs = list(images or [])
        if routing_mode == "chat":
            answer = self._run_chat_agent(
                question,
                session_context,
                images=image_inputs,
                trace=trace,
            )
            route = self._route_for_selected(
                "chat",
                "Session routing_mode='chat' forced the conversational path.",
                confidence=1.0,
            )
            return "chat", answer, None, None, route

        capabilities = self._build_capabilities_context(routing_mode=routing_mode)
        observations: list[dict[str, Any]] = []
        selected = "chat"
        error_info: dict[str, Any] | None = None
        route = trace.route

        for step in range(self._agent_max_steps()):
            self._raise_if_cancelled("planner_before")
            try:
                action = self._plan_next_action(
                    question=question,
                    session_context=session_context,
                    file_context=file_context,
                    images=image_inputs,
                    capabilities=capabilities,
                    observations=observations,
                )
            except UnsupportedPlannerActionError as unsupported:
                # Bounded re-ask: surface the rejected action back to the
                # planner as a structured observation and let it decide.
                observations.append(
                    {
                        "step": step + 1,
                        "type": "planner_error",
                        "ok": False,
                        "result": {
                            "message": str(unsupported),
                            "action": unsupported.action,
                        },
                    }
                )
                continue
            except RoutingError as planner_error:
                if not self._has_successful_execution_observation(observations):
                    raise
                answer = self._synthesize_agent_answer(
                    question=question,
                    session_context=session_context,
                    images=image_inputs,
                    observations=observations,
                )
                trace_selected = self._selected_expert_from_trace(trace)
                if selected == "chat" and trace_selected != "chat":
                    selected = trace_selected
                route = self._route_for_selected(
                    selected,
                    "Agent planner failed after completed tool observations; "
                    "CLIO answered from accumulated observations.",
                    confidence=0.55,
                )
                planner_error_details = planner_error.details
                planner_original_error = (
                    self._coerce_text(planner_error_details.get("original_error")).strip()
                    if isinstance(planner_error_details, dict)
                    else ""
                )
                error_info = RoutingError(
                    "Agent planner failed after completed tool observations.",
                    details=self._recovery_details(
                        partial=True,
                        stage="post_observation_planning",
                        original_error=planner_original_error or str(planner_error),
                        planner_error=planner_error.to_dict(),
                        planner_observations=observations[-3:],
                    ),
                ).to_dict()
                return selected, answer, None, error_info, route
            self._raise_if_cancelled("planner_after")
            kind = self._coerce_text(action.get("action")).strip().lower()
            reason = self._coerce_text(action.get("reason")).strip()

            if kind == "tool":
                tool_name = self._coerce_text(action.get("tool")).strip()
                owning_expert = self._selected_expert_for_tool(tool_name)
                selected = self._parent_route_for_child(owning_expert) or owning_expert
                scope_error = self._tool_action_scope_error(
                    tool_name,
                    selected=selected,
                    question=question,
                    file_context=file_context,
                    session_context=session_context,
                )
                if scope_error is not None:
                    observations.append(
                        {
                            "step": step + 1,
                            "type": "planner_error",
                            "ok": False,
                            "result": scope_error,
                        }
                    )
                    continue
                self._raise_if_cancelled("tool_before")
                try:
                    result = self._execute_tool_action(
                        tool_name,
                        action.get("args"),
                        trace,
                        question=question,
                        file_context=file_context,
                        session_context=session_context,
                    )
                except TypeError as exc:
                    if "unexpected keyword argument" not in str(exc):
                        raise
                    result = self._execute_tool_action(tool_name, action.get("args"), trace)
                self._raise_if_cancelled("tool_after")
                route = self._route_for_selected(
                    selected,
                    reason or f"Agent planner called tool {tool_name}.",
                    confidence=0.75,
                )
                observations.append(
                    {
                        "step": step + 1,
                        "type": "tool",
                        "tool": tool_name,
                        "ok": tool_result_ok(result),
                        "result": compact_tool_result(
                            result,
                            tool=tool_name,
                            ok=tool_result_ok(result),
                        ),
                    }
                )
                continue

            if kind == "none":
                if routing_mode == "experts":
                    raise RoutingError(
                        "Session routing_mode='experts' rejected the planner's no-op route.",
                        details=self._recovery_details(
                            requested_mode=routing_mode,
                            planner_action=action,
                        ),
                    )
                answer = self._coerce_text(action.get("answer")).strip()
                if not answer:
                    raise RoutingError(
                        "Agent planner selected no action but did not provide an explanation.",
                        details=self._recovery_details(planner_action=action),
                    )
                route = self._route_for_selected(
                    "none",
                    reason or "Agent planner found no suitable CLIO action.",
                    confidence=0.7,
                )
                return "none", answer, None, None, route

            if kind == "answer":
                if routing_mode == "experts" and not observations:
                    raise RoutingError(
                        "Session routing_mode='experts' rejected a direct planner answer.",
                        details=self._recovery_details(
                            requested_mode=routing_mode,
                            planner_action=action,
                        ),
                    )
                answer = self._coerce_text(action.get("answer")).strip()
                if not answer and observations:
                    answer = self._synthesize_agent_answer(
                        question=question,
                        session_context=session_context,
                        images=image_inputs,
                        observations=observations,
                    )
                if not answer:
                    raise RoutingError(
                        "Agent planner selected a direct answer but did not provide usable text.",
                        details=self._recovery_details(planner_action=action),
                    )
                if selected == "chat":
                    selected = self._selected_expert_from_trace(trace)
                route = self._route_for_selected(
                    selected,
                    reason or "Agent planner answered from conversation or observations.",
                    confidence=0.7,
                )
                return selected, answer, None, None, route

            observations.append(
                {
                    "step": step + 1,
                    "type": "planner_error",
                    "ok": False,
                    "result": {
                        "message": f"Planner returned unsupported action {kind!r}.",
                        "action": action,
                    },
                }
            )

        if not observations or all(obs.get("type") == "planner_error" for obs in observations):
            raise RoutingError(
                "Agent planner reached the step limit without producing a valid action.",
                details=self._recovery_details(
                    step_limit=self._agent_max_steps(),
                    planner_observations=observations[-3:],
                ),
            )

        self._raise_if_cancelled("answer_synthesis_before")
        answer = self._synthesize_agent_answer(
            question=question,
            session_context=session_context,
            images=image_inputs,
            observations=observations,
        )
        self._raise_if_cancelled("answer_synthesis_after")
        if selected == "chat":
            selected = self._selected_expert_from_trace(trace)
        route = self._route_for_selected(
            selected,
            "Agent planner reached the step limit and answered from accumulated observations.",
            confidence=0.55,
        )
        error_info = RoutingError(
            "Agent planner reached the step limit after partial observations.",
            details=self._recovery_details(
                partial=True,
                stage="step_limit_after_observations",
                step_limit=self._agent_max_steps(),
                planner_observations=observations[-3:],
            ),
        ).to_dict()
        return selected, answer, None, error_info, route

    @staticmethod
    def _has_successful_execution_observation(observations: list[dict[str, Any]]) -> bool:
        """Return whether the loop has a completed non-planner observation to answer from."""
        return any(
            observation.get("type") != "planner_error" and observation.get("ok") is True
            for observation in observations
        )

    def _plan_next_action(
        self,
        *,
        question: str,
        session_context: str,
        file_context: str,
        images: list[Any] | None = None,
        capabilities: str,
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Ask the planner for a validated JSON action.

        All providers, including local OpenAI-compatible backends, must
        route through DSPy/LiteLLM. Adapter or provider failures are
        surfaced as routing errors rather than retried through a raw
        HTTP side channel with different semantics.
        """
        observations_text = self._format_observations_for_prompt(observations)
        planner_context = self._planner_session_context(session_context)
        image_inputs = list(images or [])
        try:
            result = self._call_with_transient_provider_retries(
                "action_planner",
                lambda: self._call_action_planner(
                    question=self._planner_question(question),
                    images=image_inputs,
                    session_context=planner_context,
                    file_context=file_context,
                    capabilities=capabilities,
                    observations=observations_text,
                ),
            )
            return self._parse_action_json(getattr(result, "action_json", ""))
        except UnsupportedPlannerActionError:
            # Well-formed JSON with a non-executable kind is not a format
            # failure: the agent loop re-asks the planner with a structured
            # planner_error observation instead of a compact-prompt retry.
            raise
        except Exception as planner_error:
            raw_action = self._parse_action_from_adapter_error(planner_error)
            if raw_action is not None:
                return raw_action
            retry_capabilities = self._compact_planner_capabilities(capabilities)
            if retry_capabilities != capabilities:
                try:
                    result = self._call_with_transient_provider_retries(
                        "action_planner_compact",
                        lambda: self._call_action_planner(
                            question=self._planner_retry_question(question),
                            images=image_inputs,
                            session_context=planner_context,
                            file_context=file_context,
                            capabilities=retry_capabilities,
                            observations=observations_text,
                        ),
                    )
                    return self._parse_action_json(getattr(result, "action_json", ""))
                except UnsupportedPlannerActionError:
                    raise
                except Exception as retry_error:
                    raw_action = self._parse_action_from_adapter_error(retry_error)
                    if raw_action is not None:
                        return raw_action
                    if self.verbose:
                        print(f"[Planner] DSPy compact planner failed: {retry_error}")
                    raise RoutingError(
                        "Agent planner failed to produce an action.",
                        details={
                            "original_error": str(planner_error),
                            "retry_error": str(retry_error),
                        },
                    ) from retry_error
            if self.verbose:
                print(f"[Planner] DSPy planner failed: {planner_error}")
            raise RoutingError(
                "Agent planner failed to produce an action.",
                details={"original_error": str(planner_error)},
            ) from planner_error

    def _call_action_planner(
        self,
        *,
        question: str,
        images: list[Any] | None = None,
        session_context: str,
        file_context: str,
        capabilities: str,
        observations: str,
    ) -> Any:
        """Invoke the DSPy/LiteLLM action planner."""
        with dspy.context(lm=self._planner_lm, adapter=self._dspy_adapter):
            return self.action_planner(
                question=question,
                images=list(images or []),
                session_context=session_context,
                file_context=file_context or "No current file context",
                capabilities=capabilities,
                observations=observations,
            )

    @classmethod
    def _planner_session_context(cls, session_context: str) -> str:
        """Return ARC session context suitable for planner routing prompts.

        ContextCompiler enriches expert context with an ``[Available Tools]``
        section. The action planner already receives the live registry/tool
        capability context separately, so duplicating tool lists here makes
        local structured-output models more likely to spend their small
        planner budget on repeated capability text instead of the JSON action.
        """
        return cls._strip_context_sections(session_context, {"Available Tools"})

    @classmethod
    def _chat_session_context(cls, session_context: str) -> str:
        """Return ARC session context suitable for conversational answers.

        Chat answers are plain LM synthesis, not a tool execution surface. Keep
        prior conversation, data, analysis, and routing context, but remove the
        global tool catalog so chat does not claim direct ownership of tools
        that are actually routed through planner-selected experts.
        """
        return cls._strip_context_sections(session_context, {"Available Tools"})

    @staticmethod
    def _strip_context_sections(session_context: str, excluded: set[str]) -> str:
        """Remove named bracketed sections from compiled ARC context."""
        text = session_context.strip()
        if not text:
            return "No prior context"

        output: list[str] = []
        skipping = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section_name = stripped[1:-1].strip()
                skipping = section_name in excluded
                if not skipping:
                    output.append(line)
                continue
            if not skipping:
                output.append(line)

        cleaned = "\n".join(output).strip()
        return cleaned or "No prior context"

    def _planner_question(self, question: str) -> str:
        """Return the user question with planner-only local reasoning controls."""
        if not self._uses_no_think_planner_profile():
            return question
        return (
            "/no_think\n"
            "Return only the action_json JSON object. Do not include reasoning, "
            "analysis, markdown, or prose outside the JSON object.\n"
            f"{question}"
        )

    def _planner_retry_question(self, question: str) -> str:
        """Return a stricter planner prompt for compact retry attempts."""
        if not self._uses_no_think_planner_profile():
            return question
        return (
            "/no_think\n"
            "Return exactly one minified JSON action: a listed tool call, an "
            "answer, or none.\n"
            f"{question}"
        )

    @staticmethod
    def _compact_planner_capabilities(capabilities: str) -> str:
        """Shorten capability text for a structured-output retry."""
        lines: list[str] = []
        in_tools = False
        for raw_line in capabilities.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in {"Scoped tools:", "Chat utility tools:"}:
                in_tools = True
                lines.append(line)
                continue
            if line in {"Experts:", "Tool scope rules:"}:
                in_tools = False
                lines.append(line)
                continue
            if line.startswith("Routing strategy:"):
                in_tools = False
                lines.append(line)
                continue
            if line.startswith("Routing override:"):
                in_tools = False
                lines.append(line)
                continue
            if in_tools and line.startswith("- ") and line.endswith(":"):
                lines.append(line)
                continue
            if in_tools and line.startswith("- "):
                lines.append(line.split(":", 1)[0])
                continue
            if line.startswith("- ") and "; tools:" in line:
                expert_part, tools_part = line.split("; tools:", 1)
                tools = ", ".join(tool.strip() for tool in tools_part.split(",") if tool.strip())
                lines.append(f"{expert_part}; tools: {tools}")
                continue
            lines.append(line)
        compacted = "\n".join(lines).strip()
        return compacted or capabilities

    def _uses_no_think_planner_profile(self) -> bool:
        """Return whether the configured local planner supports /no_think control."""
        provider = self._coerce_text(getattr(self._provider_config, "provider", "")).lower()
        if provider not in {"lm_studio", "ollama"}:
            return False
        model = self._coerce_text(getattr(self._provider_config, "model", "")).lower()
        normalized = model.replace("_", "-")
        return any(
            marker in normalized
            for marker in (
                "qwopus",
                "qwen3",
                "qwen-3",
                "qwen35",
                "qwen-3.5",
            )
        )

    def _execute_tool_action(
        self,
        tool_name: str,
        raw_args: Any,
        trace: RunTrace,
        *,
        question: str = "",
        file_context: str = "",
        session_context: str = "",
    ) -> Any:
        """Execute a planner-selected tool and record provenance."""
        args = self._normalize_tool_args(raw_args)
        args = self._repair_filepath_arg_from_context(
            args,
            question=question,
            file_context=file_context,
            session_context=session_context,
        )
        known_tools = self._known_tool_names()

        if not tool_name or tool_name not in known_tools:
            return {
                "error": normalize_tool_error(
                    f"Planner selected unknown tool {tool_name!r}.",
                    tool=tool_name or None,
                    code="unknown_tool",
                    next_action="Choose one of the tools listed in capabilities.",
                    details={"available": sorted(known_tools)},
                )
            }

        owner = self._selected_expert_for_tool(tool_name)
        start = time.time()
        try:
            raw_result = self._active_tool_executor().call_tool(tool_name, args)
            result = normalize_tool_result(self._decode_tool_result(raw_result), tool=tool_name)
        except CancellationError:
            raise
        except Exception as exc:  # noqa: BLE001 - error surfaced in result['error'] via normalize_tool_error
            result = {"error": normalize_tool_error(exc, tool=tool_name, code="tool_exception")}
        duration_ms = (time.time() - start) * 1000
        trace.record_tool(
            tool=tool_name,
            params=args,
            result=result,
            duration_ms=duration_ms,
            ok=tool_result_ok(result),
        )
        self._record_direct_tool_handoff(
            trace,
            expert_id=owner,
            tool_name=tool_name,
            args=args,
            result=result,
            duration_ms=duration_ms,
        )
        return result

    def _record_direct_tool_handoff(
        self,
        trace: RunTrace,
        *,
        expert_id: str,
        tool_name: str,
        args: Mapping[str, Any],
        result: Any,
        duration_ms: float,
    ) -> None:
        """Record the expert boundary for a planner-selected direct tool call."""
        if not expert_id:
            return
        self._record_expert_handoff(
            trace,
            expert_id=expert_id,
            dispatch_target=tool_name,
            stage="direct_tool",
            input_summary=f"{tool_name}({', '.join(sorted(str(key) for key in args))})",
            result=result,
            status="success" if tool_result_ok(result) else "failure",
            duration_ms=duration_ms,
            error=None if tool_result_ok(result) else self._coerce_text(result),
        )

    def _tool_action_scope_error(
        self,
        tool_name: str,
        *,
        selected: str,
        question: str,
        file_context: str,
        session_context: str,
    ) -> dict[str, Any] | None:
        """Return a planner validation error for an out-of-scope tool action."""

        if tool_name != "shell_bash":
            return None
        if self._question_allows_shell_tool(question, file_context):
            return None

        paths = extract_file_paths(
            question,
            "\n".join(part for part in (file_context, session_context) if part),
            SCIENTIFIC_FILE_SUFFIXES,
        )
        if not paths:
            return None

        native_tools = [
            name
            for name in sorted(self._known_tool_names())
            if tool_visible_to(name, selected) and name != tool_name
        ]
        return {
            "message": (
                "Planner selected shell_bash for a scientific file request, but shell_bash "
                "is scoped to utility diagnostics and must not be used as a data-inspection "
                "shortcut."
            ),
            "tool": tool_name,
            "selected_expert": selected,
            "detected_files": [str(path) for path in paths],
            "next_action": (
                "Use the appropriate native scientific tool from available_scoped_tools instead."
            ),
            "available_scoped_tools": native_tools,
        }

    @staticmethod
    def _question_allows_shell_tool(
        question: str,
        file_context: str = "",
        session_context: str = "",
    ) -> bool:
        """Return whether the user explicitly asked for local shell diagnostics."""

        del session_context
        text = " ".join((question, file_context)).lower()
        for path in extract_file_paths(question, file_context, SCIENTIFIC_FILE_SUFFIXES):
            text = text.replace(str(path).lower(), " ")
        phrase_terms = (
            "bash",
            "shell",
            "terminal",
            "command line",
            "run command",
            "execute command",
            "cwd",
            "working directory",
            "current directory",
            "environment variable",
            "env var",
            "current time",
            "what time",
            "what day",
        )
        word_terms = ("date", "today", "whoami", "hostname")
        return any(term in text for term in phrase_terms) or any(
            re.search(rf"\b{re.escape(term)}\b", text) for term in word_terms
        )

    @classmethod
    def _file_diffs_from_trace(
        cls,
        trace: RunTrace,
        *,
        edit_mode: str = "diff",
    ) -> list[dict[str, Any]]:
        """Return GACT file_diff rows produced by successful propose_edit tools."""

        rows: list[dict[str, Any]] = []
        for observation in trace.tools:
            if not observation.ok or not observation.tool.endswith("propose_edit"):
                continue
            if not isinstance(observation.result, Mapping):
                continue
            path = cls._coerce_text(observation.result.get("path")).strip()
            unified_diff = cls._coerce_text(observation.result.get("unified_diff"))
            new_content = cls._coerce_text(observation.result.get("new_content"))
            if not new_content:
                new_content = cls._coerce_text(observation.params.get("new_content"))
            if not path or (not unified_diff and not new_content):
                continue
            rows.append(
                {
                    "path": path,
                    "unified_diff": unified_diff,
                    "new_content": new_content,
                    "edit_mode": edit_mode,
                    "lines_added": int(observation.result.get("lines_added", 0) or 0),
                    "lines_removed": int(observation.result.get("lines_removed", 0) or 0),
                }
            )
        return rows

    def _synthesize_agent_answer(
        self,
        *,
        question: str,
        session_context: str,
        images: list[Any] | None = None,
        observations: list[dict[str, Any]],
    ) -> str:
        """Produce a final answer from observations or surface synthesis failure."""
        self._raise_if_cancelled("answer_synthesis_before")
        observations_text = self._format_observations_for_prompt(observations)
        answer_question = self._answer_synthesis_question(question)
        try:
            with dspy.context(lm=self._main_lm, adapter=self._dspy_adapter):
                result = self._call_with_transient_provider_retries(
                    "answer_synthesizer",
                    lambda: self.answer_synthesizer(
                        question=answer_question,
                        images=list(images or []),
                        session_context=session_context,
                        observations=observations_text,
                    ),
                )
            answer = self._coerce_text(getattr(result, "answer", "")).strip()
            self._raise_if_cancelled("answer_synthesis_after")
            if answer:
                return answer
        except CancellationError:
            raise
        except Exception as exc:
            recovered = self._parse_answer_from_adapter_error(exc)
            if recovered:
                self._raise_if_cancelled("answer_synthesis_after")
                return recovered
            fallback = (
                self._fallback_answer_from_observations(observations)
                if self._can_fallback_after_synthesis_exception(exc)
                else ""
            )
            if fallback:
                self._raise_if_cancelled("answer_synthesis_after")
                return fallback
            if self.verbose:
                print(f"[Planner] Answer synthesis failed: {exc}")
            raise ProviderError(
                "CLIO could not synthesize a final answer from the completed observations.",
                details=self._recovery_details(
                    stage="answer_synthesis",
                    original_error=str(exc),
                    observations=observations[-3:],
                ),
            ) from exc
        fallback = self._fallback_answer_from_observations(observations)
        if fallback:
            self._raise_if_cancelled("answer_synthesis_after")
            return fallback
        raise ProviderError(
            "CLIO could not synthesize a final answer from the completed observations.",
            details=self._recovery_details(
                stage="answer_synthesis",
                original_error="answer synthesizer returned an empty answer",
                observations=observations[-3:],
            ),
        )

    @classmethod
    def _fallback_answer_from_observations(cls, observations: list[dict[str, Any]]) -> str:
        """Return a compact answer grounded only in successful observations."""
        lines: list[str] = []
        for observation in observations:
            if observation.get("ok") is False:
                continue
            tool = cls._coerce_text(observation.get("tool")).strip()
            result = observation.get("result")
            if not tool or result in (None, ""):
                continue
            if isinstance(result, dict) and "error" in result:
                continue

            scalar_summary = cls._scalar_observation_summary(result)
            if scalar_summary:
                lines.append(f"{tool} returned: {scalar_summary}.")
            else:
                lines.append(f"{tool} returned a successful result.")

            try:
                lines.append(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            except TypeError:
                lines.append(cls._coerce_text(result))

        return "\n".join(line for line in lines if line).strip()

    @staticmethod
    def _can_fallback_after_synthesis_exception(exc: Exception) -> bool:
        """Return whether completed observations may replace failed synthesis.

        Provider/runtime failures should still surface as errors. This fallback
        is only for adapter formatting failures where tools already completed
        and the provider returned unusable structured output.
        """
        text = str(exc).lower()
        return "expected to find output fields" in text or "failed to parse" in text

    def _call_with_transient_provider_retries(
        self,
        label: str,
        call: Callable[[], Any],
    ) -> Any:
        """Call a provider-backed function with bounded transient-error backoff."""
        delays = self._transient_provider_retry_delays()
        attempt = 0
        while True:
            try:
                return call()
            except CancellationError:
                raise
            except Exception as exc:
                if not self._is_transient_provider_error(exc) or attempt >= len(delays):
                    raise
                delay_s = delays[attempt]
                attempt += 1
                if self.verbose:
                    print(
                        f"[Provider] {label} transient failure; "
                        f"retry {attempt}/{len(delays)} after {delay_s:g}s: {exc}"
                    )
                if delay_s > 0:
                    time.sleep(delay_s)

    @staticmethod
    def _is_transient_provider_error(exc: Exception) -> bool:
        """Return whether an exception looks like a retryable provider throttle/outage."""
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "ratelimit",
                "rate limit",
                "tokens/minute",
                "429",
                "too many requests",
                "temporarily unavailable",
                "service unavailable",
            )
        )

    @staticmethod
    def _transient_provider_retry_delays() -> tuple[float, ...]:
        """Return configured transient provider retry delays in seconds."""
        items: list[str] | None = conf.resolve(
            "limits.transient_provider_retry_delays",
            env="CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS",
            default=None,
            cast=conf.as_csv,
        )
        if items is None:
            # conf treats a set-but-empty env var as "unset" (it falls through
            # to the default), but this knob's documented disable contract is
            # that CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS="" (set, empty) turns
            # retries OFF. Preserve it: only a truly absent variable falls back
            # to the 5s/15s default. A file-layer value still wins when present
            # (resolve returned it above before we ever got here).
            import os  # noqa: PLC0415 - only needed on this fallthrough

            raw_env = os.environ.get("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS")
            if raw_env is not None and raw_env.strip() == "":
                return ()
            items = ["5", "15"]
        # Explicit disable sentinel (a lone false/off/none/disabled token) -> no
        # retries.
        if len(items) == 1 and str(items[0]).strip().lower() in {
            "false",
            "off",
            "none",
            "disabled",
        }:
            return ()
        delays: list[float] = []
        for item in items:
            token = str(item).strip()
            if not token:
                continue
            try:
                delay = float(token)
            except ValueError:
                continue
            delays.append(max(0.0, min(delay, 60.0)))
        return tuple(delays)

    @classmethod
    def _scalar_observation_summary(cls, value: Any) -> str:
        """Return key=value text for top-level scalar observation fields."""
        if not isinstance(value, dict):
            return ""
        scalars: list[str] = []
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)) or item is None:
                scalars.append(f"{key}={item}")
        return ", ".join(scalars[:8])

    def _answer_synthesis_question(self, question: str) -> str:
        """Return provider-profile-specific instructions for answer synthesis."""
        if not self._uses_no_think_planner_profile():
            return question
        return (
            "/no_think\n"
            "Answer from the observations only. Do not include reasoning or hidden "
            "analysis. Return visible final answer text in the answer field; never "
            "leave it empty when tool observations succeeded.\n\n"
            f"User question: {question}"
        )

    @staticmethod
    def _recovery_details(**details: Any) -> dict[str, Any]:
        """Attach client-facing recovery actions to a structured error."""
        return {**details, "recovery_actions": list(ERROR_RECOVERY_ACTIONS)}

    @classmethod
    def _with_recovery_actions(cls, error_info: dict[str, Any]) -> dict[str, Any]:
        """Ensure a serialized error advertises retry/reconfigure/exit options."""
        details = error_info.get("details")
        if isinstance(details, dict):
            details.setdefault("recovery_actions", list(ERROR_RECOVERY_ACTIONS))
        else:
            error_info["details"] = cls._recovery_details()
        return error_info

    @staticmethod
    def _raise_if_cancelled(stage: str) -> None:
        """Raise a structured cancellation if the active turn was cancelled."""
        if cancellation_requested():
            raise CancellationError(
                "turn cancelled by client",
                details={
                    "execution_cancellation": "cooperative",
                    "executor_work_may_continue": False,
                    "stage": stage,
                },
            )

    def _effective_routing_mode(self) -> str:
        """Return the active GACT routing override, if one is set."""
        mode = str(_ROUTING_MODE_OVERRIDE.get() or "").strip().lower() or "auto"
        if mode in {"auto", "chat", "experts", "reasoning_only"}:
            return mode
        return "auto"

    def _build_capabilities_context(self, routing_mode: str = "auto") -> str:
        """Describe live experts and scoped tools for the planner."""
        available_tools = {
            tool.name: tool for tool in sorted(self._available_dspy_tools(), key=lambda t: t.name)
        }
        lines = ["Experts:"]
        for agent_id in self.registry.list_root_agents(planner_visible_only=True):
            caps = self.registry.get_capabilities(agent_id)
            if caps is None:
                continue
            tools = ", ".join(caps.tools) if caps.tools else "no direct tools"
            metadata_notes = self._planner_capability_metadata_notes(caps.metadata)
            metadata_text = f"; {metadata_notes}" if metadata_notes else ""
            lines.append(f"- {agent_id}: {caps.description}{metadata_text}; tools: {tools}")
            child_lines = self._planner_child_capability_lines(agent_id)
            lines.extend(f"  {line}" for line in child_lines)

        lines.append("Scoped tools:")
        for agent_id in self.registry.list_root_agents(planner_visible_only=True):
            caps = self.registry.get_capabilities(agent_id)
            if caps is None:
                continue
            scoped_lines = [
                self._planner_tool_line(tool_name, available_tools)
                for tool_name in caps.tools
                if tool_visible_to(tool_name, agent_id)
            ]
            scoped_lines = [line for line in scoped_lines if line]
            child_scoped_lines = self._planner_child_tool_lines(agent_id, available_tools)
            if not scoped_lines and not child_scoped_lines:
                continue
            lines.append(f"- {agent_id}:")
            lines.extend(f"  {line}" for line in scoped_lines)
            lines.extend(f"  {line}" for line in child_scoped_lines)

        chat_utility_lines = [
            self._planner_tool_line(tool_name, available_tools)
            for tool_name in sorted(available_tools)
            if tool_visible_to(tool_name, "chat")
        ]
        chat_utility_lines = [line for line in chat_utility_lines if line]
        if chat_utility_lines:
            lines.append("Chat utility tools:")
            lines.extend(chat_utility_lines)

        lines.append("Tool scope rules:")
        lines.append(
            "- Tools are owned by the expert section they are listed under; do not treat "
            "scientific data, analysis, visualization, and utility tools as one shared pool."
        )
        lines.append(
            "- Direct tool actions are allowed only for listed tool names and are attributed "
            "to the owning expert. Chat may only use tools listed under Chat utility tools."
        )
        lines.append(
            "- Child experts are delegated capabilities owned by their parent expert. "
            "Call their listed tools directly; CLIO attributes the work to the owning "
            "hierarchy."
        )
        lines.append(
            "Routing strategy: choose the tool that resolves the next unresolved phase, "
            "not the final deliverable. For multi-phase work, run one phase, observe "
            "its result, then plan the next phase from the updated state. Do not skip "
            "data acquisition/discovery before analysis, and do not skip analysis "
            "before visualization."
        )
        lines.append(
            "Observation rule: local_paths in observations are newly available files. "
            "Use them for the next phase instead of repeating the same discovery or "
            "staging tool, while preserving source/provenance caveats."
        )
        if routing_mode == "experts":
            lines.append(
                "Routing override: experts mode is active. Do not choose answer or none "
                "before a tool has produced an observation."
            )
        return "\n".join(lines)

    def _planner_child_capability_lines(self, parent_id: str) -> list[str]:
        """Return planner-facing child summaries nested under a parent expert."""
        lines: list[str] = []
        for child_id in self.registry.list_child_agents(parent_id):
            caps = self.registry.get_capabilities(child_id)
            if caps is None or not caps.planner_visible:
                continue
            tools = ", ".join(caps.tools) if caps.tools else "no direct tools"
            metadata_notes = self._planner_capability_metadata_notes(caps.metadata)
            metadata_text = f"; {metadata_notes}" if metadata_notes else ""
            lines.append(
                f"delegated child {child_id}: {caps.description}{metadata_text}; tools: {tools}"
            )
        return lines

    def _planner_child_tool_lines(
        self,
        parent_id: str,
        available_tools: Mapping[str, dspy.Tool],
    ) -> list[str]:
        """Return child tool descriptions nested under their parent expert."""
        lines: list[str] = []
        for child_id in self.registry.list_child_agents(parent_id):
            caps = self.registry.get_capabilities(child_id)
            if caps is None or not caps.planner_visible:
                continue
            scoped_lines = [
                self._planner_tool_line(tool_name, available_tools)
                for tool_name in caps.tools
                if tool_visible_to(tool_name, child_id)
            ]
            scoped_lines = [line for line in scoped_lines if line]
            if not scoped_lines:
                continue
            lines.append(f"delegated child {child_id}:")
            lines.extend(f"  {line}" for line in scoped_lines)
        return lines

    def _planner_tool_line(self, tool_name: str, available_tools: Mapping[str, dspy.Tool]) -> str:
        """Return one planner-facing tool signature line if the tool is callable."""
        tool = available_tools.get(tool_name)
        if tool is None:
            return ""
        arg_names = ", ".join(sorted((getattr(tool, "args", {}) or {}).keys()))
        desc = self._first_sentence(self._coerce_text(getattr(tool, "desc", "")))
        tags = ", ".join(sorted(tool_tags(tool_name)))
        tag_text = f"; tags: {tags}" if tags else ""
        if arg_names:
            return f"- {tool.name}({arg_names}): {desc}{tag_text}"
        return f"- {tool.name}: {desc}{tag_text}"

    @staticmethod
    def _planner_capability_metadata_notes(metadata: Mapping[str, Any]) -> str:
        """Return compact registry metadata notes for planner capability text."""
        notes: list[str] = []
        suffixes = [
            str(suffix) for suffix in metadata.get("file_suffixes", []) if str(suffix).strip()
        ]
        if suffixes:
            notes.append(f"direct files: {', '.join(suffixes)}")
        coordinated = [
            str(suffix)
            for suffix in metadata.get("coordinated_file_suffixes", [])
            if str(suffix).strip()
        ]
        if coordinated:
            notes.append(f"coordinates multi-file bundles: {', '.join(coordinated)}")
        intents = [
            str(intent) for intent in metadata.get("coordinator_intents", []) if str(intent).strip()
        ]
        if intents:
            notes.append(f"coordination intents: {', '.join(intents)}")
        delegates = [
            str(agent_id) for agent_id in metadata.get("delegates_to", []) if str(agent_id).strip()
        ]
        if delegates:
            notes.append(f"delegates to: {', '.join(delegates)}")
        return "; ".join(notes)

    def _available_dspy_tools(self) -> list[dspy.Tool]:
        """Return gateway tools visible to the planner."""
        return [
            tool
            for tool in self._active_tool_executor().to_dspy_tools()
            if tool.name not in PLANNER_HIDDEN_TOOL_NAMES
        ]

    def _known_tool_names(self) -> set[str]:
        """Return every tool name currently visible to the planner."""
        return set(self._active_tool_executor().get_tool_names())

    def _selected_expert_for_tool(self, tool_name: str) -> str:
        """Resolve a tool's owning expert from the registered capability table."""
        owner = tool_owner(tool_name)
        if owner:
            return owner
        for agent_id in self.registry.list_agents():
            caps = self.registry.get_capabilities(agent_id)
            if caps and tool_name in caps.tools:
                return agent_id
        known_tools = self._known_tool_names()
        if not tool_name or tool_name not in known_tools:
            raise RoutingError(
                f"Agent planner selected unknown tool {tool_name!r}.",
                details={
                    "tool": tool_name,
                    "available_tools": sorted(known_tools),
                },
            )
        raise RoutingError(
            f"Tool {tool_name!r} has no registered owning expert.",
            details={
                "tool": tool_name,
                "available_experts": self.registry.list_agents(),
            },
        )

    def _selected_expert_from_trace(self, trace: RunTrace) -> str:
        """Infer the public selected_expert from executed tool provenance."""
        for observation in reversed(trace.tools):
            selected = self._selected_expert_for_tool(observation.tool)
            if selected != "chat":
                return self._parent_route_for_child(selected) or selected
        return "chat"

    def _route_for_selected(
        self,
        selected: str,
        reason: str,
        confidence: float,
    ) -> RouteDecision:
        """Build the public route decision for a planner-selected handler."""
        valid_targets = self._valid_route_targets()
        if selected not in valid_targets:
            raise RoutingError(
                f"Agent planner produced invalid route target {selected!r}.",
                details={
                    "selected": selected,
                    "available_targets": sorted(valid_targets),
                },
            )
        return RouteDecision(
            target=selected,  # type: ignore[arg-type]
            source="dspy",
            reason=reason,
            confidence=confidence,
        )

    def _valid_route_targets(self) -> set[str]:
        """Return root route targets from the live agent registry plus special routes."""
        targets = {
            str(agent_id).strip().lower()
            for agent_id in self.registry.list_root_agents(planner_visible_only=True)
            if str(agent_id).strip()
        }
        targets.update(SPECIAL_ROUTE_TARGETS)
        return targets

    def _parent_route_for_child(self, expert_id: str) -> str | None:
        """Return the public parent route for a child expert, if registered."""
        caps = self.registry.get_capabilities(expert_id)
        if caps is None or not caps.parent_id:
            return None
        return caps.parent_id

    @staticmethod
    def _normalize_tool_args(raw_args: Any) -> dict[str, Any]:
        """Return a dict of tool args from planner output."""
        if isinstance(raw_args, Mapping):
            return dict(raw_args)
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                decoded = json.loads(raw_args)
            except json.JSONDecodeError:
                return {}
            if isinstance(decoded, Mapping):
                return dict(decoded)
        return {}

    @staticmethod
    def _repair_filepath_arg_from_context(
        args: dict[str, Any],
        *,
        question: str,
        file_context: str,
        session_context: str = "",
    ) -> dict[str, Any]:
        """Repair a degraded filepath arg using explicit paths from the turn context."""
        raw_filepath = ClioAgent._coerce_text(args.get("filepath")).strip()
        if not raw_filepath:
            return args
        filepath = Path(raw_filepath).expanduser()
        if filepath.exists():
            return args

        basename = filepath.name
        if not basename:
            return args

        candidates = extract_file_paths(
            question,
            "\n".join(part for part in (file_context, session_context) if part),
            SCIENTIFIC_FILE_SUFFIXES,
        )
        matches = [
            candidate
            for candidate in candidates
            if candidate.name == basename and candidate.expanduser().exists()
        ]
        if len(matches) != 1:
            return args

        repaired = dict(args)
        repaired["filepath"] = str(matches[0].expanduser())
        return repaired

    @staticmethod
    def _decode_tool_result(raw_result: Any) -> Any:
        """Decode gateway JSON text into native data when possible."""
        if isinstance(raw_result, str):
            try:
                return json.loads(raw_result)
            except json.JSONDecodeError:
                return raw_result
        return raw_result

    @classmethod
    def _parse_action_json(cls, raw: Any) -> dict[str, Any]:
        """Parse and lightly validate a planner JSON action object."""
        if isinstance(raw, Mapping):
            decoded = dict(raw)
        else:
            text = cls._coerce_text(raw).strip()
            if text.startswith("```"):
                text = text.strip("`").strip()
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            if not text.startswith("{"):
                extracted = cls._extract_json_object_text(text)
                if extracted is None:
                    raise ValueError(f"Planner returned invalid JSON action: {raw!r}")
                text = extracted
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                try:
                    decoded, end = json.JSONDecoder().raw_decode(text)
                except json.JSONDecodeError as exc:
                    repaired = cls._repair_truncated_action_json(text)
                    if repaired is None:
                        raise ValueError(f"Planner returned invalid JSON action: {raw!r}") from exc
                    decoded = repaired
                else:
                    trailing = text[end:].strip()
                    # DSPy ChatAdapter error strings can append one bracket after the LM payload.
                    if trailing != "]" and not trailing.startswith("[[ ## completed ## ]]"):
                        raise ValueError(f"Planner returned invalid JSON action: {raw!r}") from None
            if not isinstance(decoded, dict):
                raise ValueError(f"Planner action must be a JSON object: {raw!r}")

        action = cls._coerce_text(decoded.get("action")).strip().lower()
        decoded["action"] = action
        if action not in SUPPORTED_PLANNER_ACTION_KINDS:
            raise UnsupportedPlannerActionError(decoded)
        return decoded

    @classmethod
    def _repair_truncated_action_json(cls, text: str) -> dict[str, Any] | None:
        """Repair a planner JSON object that ended before final delimiters.

        This intentionally accepts only a single object that starts at the
        first character. It may close an unterminated string and missing
        brackets/braces, then normal action validation still decides whether
        the repaired object is usable.
        """

        repaired = cls._close_truncated_json(text)
        if repaired is None or repaired == text:
            return None
        try:
            decoded = json.loads(repaired)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, dict):
            return None
        return decoded

    @staticmethod
    def _close_truncated_json(text: str) -> str | None:
        """Return text with missing trailing JSON delimiters appended."""

        stack: list[str] = []
        in_string = False
        escaped = False
        string_start = -1

        for index, char in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                    string_start = -1
                continue

            if char == '"':
                in_string = True
                string_start = index
            elif char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char in {"}", "]"}:
                if not stack or stack.pop() != char:
                    return None

        # Only a truncated "reason" string may be closed: it is advisory text.
        # Closing a truncated argument value would hand a tool corrupted input.
        if in_string and ClioAgent._unterminated_string_key(text, string_start) != "reason":
            return None
        suffix = '"' if in_string else ""
        suffix += "".join(reversed(stack))
        if not suffix:
            return None
        return text + suffix

    @staticmethod
    def _extract_json_object_text(text: str) -> str | None:
        """Extract the first balanced JSON object from model text."""
        start = text.find("{")
        if start < 0:
            return None

        stack: list[str] = []
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char in {"}", "]"}:
                if not stack or stack.pop() != char:
                    return None
                if not stack:
                    return text[start : index + 1]
        return None

    @staticmethod
    def _unterminated_string_key(text: str, string_start: int) -> str | None:
        """Return the object key for an unterminated string value."""

        prefix = text[:string_start].rstrip()
        if not prefix.endswith(":"):
            return None
        key_prefix = prefix[:-1].rstrip()
        if not key_prefix.endswith('"'):
            return None
        key_end = len(key_prefix) - 1
        key_start = key_end - 1
        while key_start >= 0:
            if key_prefix[key_start] == '"' and not ClioAgent._is_escaped(key_prefix, key_start):
                return key_prefix[key_start + 1 : key_end]
            key_start -= 1
        return None

    @staticmethod
    def _is_escaped(text: str, index: int) -> bool:
        """Return whether text[index] is escaped by an odd number of backslashes."""

        slash_count = 0
        cursor = index - 1
        while cursor >= 0 and text[cursor] == "\\":
            slash_count += 1
            cursor -= 1
        return slash_count % 2 == 1

    @classmethod
    def _parse_action_from_adapter_error(cls, error: Exception) -> dict[str, Any] | None:
        """Recover valid planner JSON from a DSPy ChatAdapter parse error.

        Weak local models sometimes follow the planner instruction to return
        exactly one JSON object, but omit DSPy's ``[[ ## action_json ## ]]``
        marker. This keeps the call on the DSPy/LiteLLM path and accepts only
        a valid CLIO action object from the already-returned LM text.
        """
        message = str(error)
        marker = "LM Response:"
        expected = "Expected to find output fields"
        if marker not in message or expected not in message or "action_json" not in message:
            return None

        raw_response = message.split(marker, 1)[1].split(expected, 1)[0].strip()
        try:
            return cls._parse_action_json(raw_response)
        except UnsupportedPlannerActionError:
            raise
        except ValueError:
            return None

    @classmethod
    def _parse_answer_from_adapter_error(cls, error: Exception) -> str | None:
        """Recover visible answer text from a DSPy answer-field parse error.

        Local models sometimes return useful prose while omitting DSPy's
        ``answer`` marker. This accepts only the already-returned model text
        and only when DSPy was explicitly expecting an ``answer`` field.
        """
        if not cls._is_answer_adapter_parse_error(error):
            return None
        message = str(error)
        marker = "LM Response:"
        expected = "Expected to find output fields"

        raw_response = message.split(marker, 1)[1].split(expected, 1)[0].strip()
        if not raw_response:
            return None
        raw_response = re.sub(r"\[\[\s*##\s*completed\s*##\s*\]\]", "", raw_response).strip()
        raw_response = re.sub(
            r"^\[\[\s*##\s*answer\s*##\s*(?:\]\])?\s*",
            "",
            raw_response,
        ).strip()
        if raw_response.startswith("[[") or raw_response in {"[", "]"}:
            return None
        return raw_response or None

    @staticmethod
    def _is_answer_adapter_parse_error(error: Exception) -> bool:
        """Return whether an exception contains an unparsed answer-field response."""
        message = str(error)
        marker = "LM Response:"
        expected = "Expected to find output fields"
        if marker not in message or expected not in message:
            return False
        expected_fields = message.split(expected, 1)[1].lower()
        return "answer" in expected_fields

    def _format_observations_for_prompt(self, observations: list[dict[str, Any]]) -> str:
        """Format loop observations as compact JSON for planner prompts."""
        if not observations:
            return "No observations yet"
        return json.dumps(observations, ensure_ascii=False, indent=2)

    @staticmethod
    def _first_sentence(text: str, max_chars: int = 220) -> str:
        """Return a compact one-line description."""
        compact = " ".join(text.split())
        if "." in compact:
            compact = compact.split(".", 1)[0] + "."
        if len(compact) > max_chars:
            compact = compact[: max_chars - 3] + "..."
        return compact

    @staticmethod
    def _agent_max_steps() -> int:
        """Read the planner loop step budget from configuration."""
        try:
            value = conf.resolve(
                "limits.agent_max_steps",
                env="CLIO_AGENT_MAX_STEPS",
                default=DEFAULT_AGENT_MAX_STEPS,
                cast=conf.as_int,
            )
        except (ValueError, TypeError):
            value = DEFAULT_AGENT_MAX_STEPS
        return max(1, min(value, 12))

    def _record_expert_handoff(
        self,
        trace: RunTrace,
        *,
        expert_id: str,
        dispatch_target: str,
        stage: str,
        input_summary: str,
        result: Any | None = None,
        status: Literal["success", "failure"] = "success",
        duration_ms: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Record an expert-stage handoff without relying on final route labels."""
        metadata = self._expert_result_metadata(result)
        # #880: no server-authored summary of the expert result. The expert's
        # deliverable rides ``output`` verbatim (empty here on the Tier-1 native
        # path, which has no parent-bound answer to carry); clio never synthesizes
        # a field-picked one-liner of the model's structured output.
        parent_id = self._registered_parent_id(expert_id)
        trace.record_expert_handoff(
            agent_id=expert_id,
            parent_id=parent_id,
            dispatch_target=dispatch_target,
            stage=stage,
            status=status,
            input_summary=input_summary,
            duration_ms=duration_ms,
            error=error,
            metadata=metadata,
        )

    def _registered_parent_id(self, expert_id: str) -> str | None:
        """Return a registered parent ID for an expert, if one exists."""
        caps = self.registry.get_capabilities(expert_id)
        if caps is None:
            return None
        return caps.parent_id

    @staticmethod
    def _expert_result_metadata(result: Any | None) -> dict[str, Any]:
        """Return JSON-like metadata from a native expert result."""
        metadata = getattr(result, "metadata", None)
        if isinstance(metadata, Mapping):
            return dict(metadata)
        return {}

    def _run_chat_agent(
        self,
        question: str,
        session_context: str,
        *,
        images: list[Any] | None = None,
        trace: RunTrace | None = None,
    ) -> str:
        """Generate a conversational reply through DSPy/LiteLLM."""
        self._raise_if_cancelled("chat_before")
        chat_context = self._chat_session_context(session_context)
        image_inputs = list(images or [])
        try:
            with dspy.context(lm=self._main_lm, adapter=self._dspy_adapter):
                if self._chat_should_use_utility_tools(question, session_context):
                    tool_agent = self._build_chat_tool_agent(
                        trace=trace,
                        question=question,
                        session_context=session_context,
                    )
                    result = tool_agent(
                        question=question,
                        images=image_inputs,
                        session_context=chat_context,
                    )
                else:
                    result = self.chat_agent(
                        question=question,
                        images=image_inputs,
                        session_context=chat_context,
                    )
            answer = self._coerce_text(getattr(result, "answer", None)).strip()
            self._raise_if_cancelled("chat_after")
            if answer:
                return answer
            raise ValueError("Chat agent returned an empty answer.")
        except Exception as chat_error:
            recovered = self._parse_answer_from_adapter_error(chat_error)
            if recovered:
                self._raise_if_cancelled("chat_after")
                return recovered
            if self.verbose:
                print(f"[ClioAgent] ChatAgent failed: {chat_error}")
            raise

    def _chat_should_use_utility_tools(self, question: str, session_context: str) -> bool:
        """Return whether chat should receive its scoped utility ReAct surface."""
        return bool(
            self._question_allows_shell_tool(question, session_context=session_context)
            and self._chat_visible_tool_names()
        )

    def _chat_visible_tool_names(self) -> list[str]:
        """Return tools explicitly visible to the chat utility surface."""
        return [name for name in sorted(self._known_tool_names()) if tool_visible_to(name, "chat")]

    def _build_chat_tool_agent(
        self,
        *,
        trace: RunTrace | None,
        question: str,
        session_context: str,
    ) -> Any:
        """Build a per-turn ReAct agent with only chat-visible utility tools."""
        available_tools = {tool.name: tool for tool in self._available_dspy_tools()}
        tools: list[dspy.Tool] = []
        for tool_name in self._chat_visible_tool_names():
            source_tool = available_tools.get(tool_name)
            if source_tool is None:
                continue
            tools.append(self._chat_scoped_tool(source_tool, trace, question, session_context))
        if not tools:
            raise RoutingError(
                "Chat utility tool surface is empty.",
                details=self._recovery_details(scope="chat", visible_tools=[]),
            )
        return dspy.ReAct(ChatAgentSignature, tools=tools, max_iters=3)

    def _chat_scoped_tool(
        self,
        source_tool: dspy.Tool,
        trace: RunTrace | None,
        question: str,
        session_context: str,
    ) -> dspy.Tool:
        """Wrap one chat-visible tool so it uses CLIO's normal tool path."""
        tool_name = source_tool.name

        def run(**kwargs: Any) -> Any:
            active_trace = trace or RunTrace(route=self._route_for_selected("chat", "chat", 1.0))
            return self._execute_tool_action(
                tool_name,
                kwargs,
                active_trace,
                question=question,
                file_context="",
                session_context=session_context,
            )

        return dspy.Tool(
            func=run,
            name=tool_name,
            desc=getattr(source_tool, "desc", "") or "",
            args=getattr(source_tool, "args", {}) or {},
        )

    @staticmethod
    def _coerce_text(value: Any) -> str:
        """Convert model/tool outputs to stable text without noisy serializers."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, (list, dict)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:  # noqa: BLE001 - value->JSON coercion falls back to str(); never fatal
                return str(value)

        # Pydantic v2 models: avoid warning-emitting serialization paths.
        if hasattr(value, "model_dump"):
            try:
                dumped = value.model_dump(mode="json", warnings="none")
                return json.dumps(dumped, ensure_ascii=False)
            except Exception:  # noqa: BLE001,S110 - value coercion cascade; falls through to the next strategy
                pass

        # Common chat/message object shape from LM backends.
        content = getattr(value, "content", None)
        if isinstance(content, str):
            return content

        return str(value)

    def _get_session_context(
        self,
        question: str,
        session_id: str,
        tier: int = 2,
        tool_scope: str = "none",
        include_conversation: bool = False,
    ) -> str:
        """Retrieve compiled session context from ARC Memory.

        Uses ContextCompiler pipeline (filter -> compact -> enrich -> assemble)
        with token budgets per tier. Falls back to "No prior context" on error.

        Args:
            question: User's current question
            session_id: Session identifier
            tier: Agent tier for token budget (1=planner/2K, 2=expert/4K)
            tool_scope: Agent/tool visibility scope for ARC tool summaries.
            include_conversation: Whether the compiled context should carry the
                conversation/routing sections. Defaults to ``False`` because the
                gact turn path prepends the full transcript as THE conversation
                channel — compiling the same turns here would double the token
                spend (#771).

        Returns:
            Compiled context string or "No prior context"
        """
        try:
            compiled = self.context_retriever.compile_expert_context(
                query=question,
                session_id=session_id,
                tier=tier,
                tool_scope=tool_scope,
                include_conversation=include_conversation,
            )
            if self.verbose:
                print(f"[ClioAgent] Compiled context ({len(compiled)} chars, tier={tier})")
            return compiled
        except Exception as exc:  # noqa: BLE001 - degraded context, not a failed turn
            logger.warning(
                "ARC context compilation failed; falling back to legacy retrieval "
                "reason=context_compile_failed session=%s tier=%s error=%s",
                session_id,
                tier,
                exc,
            )
            # Fallback to legacy retrieval
            try:
                arc_context = self.context_retriever.retrieve_context_for_query(
                    query=question,
                    session_id=session_id,
                    max_history=5,
                )
                if arc_context.learned_patterns:
                    context_parts = []
                    for p in arc_context.learned_patterns:
                        if hasattr(p, "pattern_data") and isinstance(p.pattern_data, dict):
                            for key, value in p.pattern_data.items():
                                if value and isinstance(value, str):
                                    context_parts.append(f"{key}: {value}")
                    if context_parts:
                        return "; ".join(context_parts[:5])
            except Exception as exc:  # noqa: BLE001 - degraded context, not a failed turn
                logger.warning(
                    "ARC legacy context retrieval failed; expert runs without prior context "
                    "reason=arc_context_unavailable session=%s error=%s",
                    session_id,
                    exc,
                )
        logger.warning(
            "no prior context recovered; expert runs cold reason=context_unavailable session=%s",
            session_id,
        )
        return "No prior context"

    def _get_file_context(self, session_id: str, active_file: Path | None = None) -> str:
        """Return the active session file reference, if any.

        Args:
            session_id: Session identifier (kept for signature stability).
            active_file: The resolved session file for this turn, if any.

        Returns:
            The ``Current session file`` line, or an empty string when no file
            is bound to the turn.
        """
        if active_file is not None:
            return f"Current session file: {active_file}"
        return ""

    def _resolve_session_file_reference(self, question: str, session_id: str) -> Path | None:
        """Return an explicit path or the most recent scientific file in the session."""
        explicit_paths = extract_file_paths(question, "", SCIENTIFIC_FILE_SUFFIXES)
        if explicit_paths:
            return explicit_paths[0]
        return self._last_session_file_path(session_id)

    def _last_session_file_path(self, session_id: str) -> Path | None:
        """Find the last local scientific file path mentioned in this session."""
        suffix_filter = SCIENTIFIC_FILE_SUFFIXES
        try:
            conv = self.arc.get_conversation(session_id)
        except Exception as exc:  # noqa: BLE001 - best-effort file lookup, degrades to None
            logger.debug(
                "session conversation lookup failed; no session file resolved "
                "reason=session_file_lookup_failed session=%s error=%s",
                session_id,
                exc,
            )
            return None
        if conv is None:
            return None

        fallback: Path | None = None
        for message in reversed(conv.messages):
            paths = extract_file_paths(message.content, "", suffix_filter)
            if paths:
                if fallback is None:
                    fallback = paths[0]
                for path in paths:
                    if path.expanduser().exists():
                        return path
        return fallback

    @staticmethod
    def _question_with_session_file(question: str, active_file: Path | None) -> str:
        """Append current file context so native tools own the facts."""
        if active_file is None:
            return question
        if extract_file_paths(question, "", SCIENTIFIC_FILE_SUFFIXES):
            return question
        return f"{question}\n\nUse this file from the current session: {active_file}"

    def _store_routing_decision(
        self,
        question: str,
        route: RouteDecision,
        session_id: str,
    ) -> None:
        """Store routing decision in the ARC conversation object.

        Args:
            question: User's query
            selected: Selected expert/handler ID
            session_id: Session identifier
        """
        try:
            routing_decision = RoutingDecision(
                timestamp=time.time(),
                query=question,
                capabilities_needed=list(route.capabilities),
                selected_agent=route.target,
                reasoning=f"{route.source}: {route.reason}",
                confidence=route.confidence,
            )

            conv = self.arc.get_conversation(session_id)
            if conv:
                conv.routing_decisions.append(routing_decision)
                conv.updated_at = time.time()
                self.arc.store_conversation(conv)
        except Exception as exc:  # noqa: BLE001 - telemetry write, not a failed turn
            logger.warning(
                "routing decision not persisted to ARC "
                "reason=routing_decision_store_failed session=%s error=%s",
                session_id,
                exc,
            )

    def _store_metrics(
        self,
        question: str,
        session_id: str,
        selected_expert: str,
        duration_ms: float,
        success: bool,
        error_msg: str | None = None,
        trace: RunTrace | None = None,
        nanoagents_spawned: list[dict[str, Any]] | None = None,
    ) -> None:
        """Store invocation metrics in ARC Memory.

        Args:
            question: User's question
            session_id: Session identifier
            selected_expert: Which expert handled the query
            duration_ms: Processing duration in milliseconds
            success: Whether the query succeeded
            error_msg: Error message if failed
        """
        invocation_id = trace.trace_id if trace else str(uuid.uuid4())
        invocation = Invocation(
            trace_id=invocation_id,
            session_id=session_id,
            parent_trace_id=None,
            agent_id=selected_expert,
            tier=1 if selected_expert in ("chat", "none") else 2,
            source="native",
            started_at=time.time() - duration_ms / 1000,
            completed_at=time.time(),
            duration_ms=duration_ms,
            status="success" if success else "failure",
            input={"query": question},
            output={"error": error_msg} if error_msg else {},
            tools_called=[tool.to_arc_tool_call() for tool in (trace.tools if trace else [])],
            nanoagents_spawned=self._to_arc_nanoagent_spawns(nanoagents_spawned or []),
            performance={"success": success, "duration_ms": duration_ms},
            storage_tier="warm",
        )
        self.arc.store_invocation(invocation)

    def _store_expert_invocation(
        self,
        question: str,
        file_context: str,
        selected: str,
        session_id: str,
        expert_result: Any,
        success: bool,
        error_msg: str | None,
        duration_ms: float,
        trace: RunTrace | None = None,
    ) -> None:
        """Store tier-2 expert invocation in ARC for optimizer training data.

        Logs detailed input/output for each expert dispatch so the
        TrainingSetGenerator can convert these to dspy.Examples.

        Args:
            question: User's question
            file_context: File context passed to expert
            selected: Selected expert ID
            session_id: Session identifier
            expert_result: dspy.Prediction from expert (or None on failure)
            success: Whether the expert call succeeded
            error_msg: Error message if failed
            duration_ms: Expert call duration in milliseconds
        """
        try:
            output_data: Dict[str, Any] = {}
            if success and expert_result is not None:
                output_data = _extract_output(expert_result)
            elif error_msg:
                output_data = {"error": str(error_msg)[:500]}

            invocation = Invocation(
                trace_id=str(uuid.uuid4()),
                session_id=session_id,
                parent_trace_id=None,
                agent_id=selected,
                tier=2,
                source="native",
                started_at=time.time() - duration_ms / 1000,
                completed_at=time.time(),
                duration_ms=duration_ms,
                status="success" if success else "failure",
                input={"question": question, "file_context": file_context},
                output=output_data,
                tools_called=[tool.to_arc_tool_call() for tool in (trace.tools if trace else [])],
                nanoagents_spawned=self._to_arc_nanoagent_spawns(
                    self._extract_nanoagents_spawned(expert_result)
                ),
                performance={"success": success, "duration_ms": duration_ms},
                storage_tier="warm",
            )
            self.arc.store_invocation(invocation)
        except Exception as exc:  # noqa: BLE001 - telemetry write, not a failed turn
            logger.warning(
                "expert invocation not persisted to ARC "
                "reason=invocation_store_failed session=%s agent=%s error=%s",
                session_id,
                selected,
                exc,
            )

    @staticmethod
    def _extract_nanoagents_spawned(prediction: Any) -> list[dict[str, Any]]:
        """Return nanoagent spawn wire rows from an expert prediction."""
        raw = getattr(prediction, "nanoagents_spawned", None)
        if not raw:
            return []

        out: list[dict[str, Any]] = []
        for spawn in raw:
            if isinstance(spawn, Mapping):
                row = dict(spawn)
            else:
                row = {
                    key: getattr(spawn, key)
                    for key in (
                        "agent_id",
                        "nanoagent_id",
                        "trace_id",
                        "input",
                        "task",
                        "answer",
                        "duration_ms",
                        "status",
                        "error",
                    )
                    if hasattr(spawn, key)
                }
            if row:
                out.append(row)
        return out

    @staticmethod
    def _to_arc_nanoagent_spawns(spawns: list[dict[str, Any]]) -> list[NanoagentSpawn]:
        """Convert GACT nanoagent wire rows into ARC invocation records."""
        out: list[NanoagentSpawn] = []
        for spawn in spawns:
            agent_id = str(
                spawn.get("nanoagent_id")
                or spawn.get("agent_id")
                or spawn.get("agent")
                or "nanoagent"
            )
            trace_id = str(spawn.get("trace_id") or spawn.get("id") or uuid.uuid4())
            task = str(spawn.get("task") or spawn.get("question") or spawn.get("input") or "")
            try:
                duration_ms = float(spawn.get("duration_ms", 0.0) or 0.0)
            except (TypeError, ValueError):
                duration_ms = 0.0
            status = str(spawn.get("status") or ("failure" if spawn.get("error") else "success"))
            out.append(
                NanoagentSpawn(
                    nanoagent_id=agent_id,
                    trace_id=trace_id,
                    task=task,
                    duration_ms=duration_ms,
                    status=status,
                )
            )
        return out

    def _store_conversation(self, question: str, answer: str, session_id: str) -> None:
        """Store conversation in ARC Memory.

        Args:
            question: User's question
            answer: Agent's answer
            session_id: Session identifier
        """
        current_time = time.time()
        msg_id_user = str(uuid.uuid4())
        msg_id_assistant = str(uuid.uuid4())

        user_msg = Message(
            message_id=msg_id_user,
            role="user",
            content=question,
            timestamp=current_time,
            metadata={"source": "clio_agent_main"},
        )

        assistant_msg = Message(
            message_id=msg_id_assistant,
            role="assistant",
            content=answer,
            timestamp=current_time,
            metadata={"agent": "main"},
        )

        existing_conv = self.arc.get_conversation(session_id)

        if existing_conv:
            existing_conv.messages.extend([user_msg, assistant_msg])
            existing_conv.updated_at = current_time
            existing_conv.last_accessed = current_time
            self.arc.store_conversation(existing_conv)
        else:
            conv = Conversation(
                session_id=session_id,
                user_id="default_user",
                created_at=current_time,
                updated_at=current_time,
                last_accessed=current_time,
                status="active",
                messages=[user_msg, assistant_msg],
                routing_decisions=[],
                metadata={"clio_agent_version": _clio_agent_version(), "arc_enabled": True},
                storage_tier="warm",
            )
            self.arc.store_conversation(conv)

    def get_arc_stats(self) -> Dict[str, Any]:
        """Get ARC memory statistics."""
        return self.arc.get_cache_stats()

    def get_session_history(self, session_id: str, limit: int = 10) -> List[Conversation]:
        """Get conversation history for session from ARC Memory."""
        return self.arc.get_conversation_history(session_id, limit=limit)

    def shutdown(self) -> None:
        """Close the persistent MCP tool executors so their stdio children are reaped (#900)."""
        from clio_agent.runtime.process_tree import close_tool_executors

        if self.verbose:
            print("[ClioAgent] Shutting down...")
        executors = [self.tool_executor, *self._workspace_tool_executors.values()]
        self._workspace_tool_executors = {}
        close_tool_executors(executors)
