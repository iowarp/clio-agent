"""
ClioAgent - Main Agent Host

The process-level HOST for CLIO's runtime resources. ClioAgent owns the
provider identity (``_main_lm`` / ``_planner_lm`` / ``_dspy_adapter``), the tool
gateway + per-workspace executors, pack/blueprint discovery, the ARC memory
plane, and the agent registry. It also carries the small chat-synthesis surface
that the session-compaction summarizer reuses.

The Tier-1 planner loop that used to live here is DELETED (#948 S4b): CLIO no
longer runs a deterministic ``ClioAgent.forward`` planner. The only mains are
blueprint ReAct agents resolved per turn; a default/``main`` session that
resolves no blueprint fails TYPED (``_NoResolvableAgent``) rather than falling
back to a legacy planner path.

Usage:
    >>> from clio_agent import ClioAgent
    >>> agent = ClioAgent()
    >>> agent.rebind_lms(provider_config)  # single writer for the LM surface
    >>> stats = agent.get_arc_stats()
"""

import contextvars
import json
import re
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List

import dspy

from clio_agent import conf
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.retrieval import ContextRetriever
from clio_agent.arc.schema import Conversation
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
    RoutingError,
)
from clio_agent.registry.registry import AgentRegistry
from clio_agent.signatures.main_agent_sig import ChatAgentSignature
from clio_agent.tools.catalog import (
    set_active_catalog,
    tool_owner,
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
from clio_agent.tools.reaper import WorkspaceExecutorReaper

_CANCELLATION_CHECKER: contextvars.ContextVar[Callable[[], bool] | None] = contextvars.ContextVar(
    "clio_cancellation_checker", default=None
)


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


class ClioAgent(dspy.Module):
    """CLIO Agent host: providers, tools, ARC, workspaces, and registry.

    ClioAgent is the process-level resource host. It binds the provider LM
    surface (``rebind_lms``), builds the tool gateway + per-workspace executors,
    discovers packs/blueprints, owns the ARC memory plane and the agent
    registry, and exposes the chat-synthesis helper used by the session-compact
    summarizer. It no longer runs a Tier-1 planner loop.

    Attributes:
        chat_agent: DSPy Predict with ChatAgentSignature (chat synthesis)
        arc: ARC Memory instance
        context_retriever: Context retrieval module
        _tool_definitions: preloaded tool defs from the boot listing pass
            (#932); None -> executors fall back to eager list_tools
        registry: Agent registry for discovery
    """

    # Class default so partially-constructed instances (test stubs) resolve it.
    _tool_definitions: dict[str, Any] | None = None
    # Guards first-touch creation of the per-instance workspace-fleet state.
    _WS_STATE_INIT_LOCK = threading.Lock()

    def _workspace_state(self) -> tuple[threading.Lock, dict[str, Any], dict[str, int]]:
        """Per-instance (lock, executors, leases), lazily created — covers
        partially-constructed test stubs that skip __init__ (#933)."""

        lock = getattr(self, "_workspace_executor_lock", None)
        if lock is None:
            with ClioAgent._WS_STATE_INIT_LOCK:
                lock = getattr(self, "_workspace_executor_lock", None)
                if lock is None:
                    if not hasattr(self, "_workspace_tool_executors"):
                        self._workspace_tool_executors: dict[str, Any] = {}
                    if not hasattr(self, "_workspace_leases"):
                        self._workspace_leases: dict[str, int] = {}
                    # The lock is the fast-path publish signal — it must be
                    # assigned LAST so no reader sees it before the dicts.
                    lock = threading.Lock()
                    self._workspace_executor_lock = lock
        return lock, self._workspace_tool_executors, self._workspace_leases

    def __init__(
        self,
        verbose: bool = False,
        data_dir: str = ".clio/agent",
        arc: ARCMemory | None = None,
        provider_config: LMProviderConfig | None = None,
    ):
        """Initialize the ClioAgent host: chat, tool execution, ARC, and runtime storage.

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

        # Bind the LM surface (``_main_lm`` / ``_planner_lm`` / ``_dspy_adapter``).
        self.rebind_lms(self._provider_config)

        # Chat Agent: Predict for conversational responses. This keeps the
        # structured output surface smaller than ChainOfThought, which is more
        # reliable with local OpenAI-compatible backends. Reused by the
        # session-compaction summarizer (routes/sessions.py).
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
        # Guarded by _workspace_executor_lock (shared with the #933 reaper);
        # _workspace_leases counts LIVE TURNS pinning a root (never reaped).
        lock, executors, leases = self._workspace_state()
        self._workspace_reaper = WorkspaceExecutorReaper(executors, lock, leases=leases)
        self._workspace_reaper.start()

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
        # #1113: wire the receive-loop elicitation handler onto every declared-server
        # backend so a mid-tool-call elicitation reaches the HITL surface. The hook is
        # app-agnostic (it resolves its invocation from the correlation record the tool
        # observer opens), so binding it once on the shared gateway is safe.
        from clio_agent.gact.elicitation_correlation import (
            make_correlated_handlers,  # noqa: PLC0415
        )

        tool_gateway = build_gateway(specs, cwd=cwd, handlers=make_correlated_handlers())
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
        lock, executors, _leases = self._workspace_state()
        with lock:
            executor = executors.get(root)
            if executor is None or getattr(executor, "closed", False):
                # First use for this root, or the #933 reaper reclaimed the
                # fleet — rebuild lazily (spawns nothing until a tool call).
                gateway = self._build_tool_gateway(cwd=root)
                executor = create_sync_tool_executor(
                    gateway,
                    preloaded_tools=self._tool_definitions,
                    namespace_servers=namespace_proxies(gateway),
                )
                executors[root] = executor
            return executor

    @contextmanager
    def lease_workspace_fleet(self, root: str):
        """Pin a workspace root against reaping for the caller's scope (#933).

        Acquired per TURN (gact's ``_tool_session_context``): DSPy tools bind
        the executor for the whole expert lifetime, so between-call idleness
        inside a live turn must never count toward the reap TTL.
        """

        root = (root or "").strip()
        if not root:
            yield
            return
        lock, _executors, leases = self._workspace_state()
        with lock:
            leases[root] = leases.get(root, 0) + 1
        try:
            yield
        finally:
            with lock:
                remaining = leases.get(root, 1) - 1
                if remaining <= 0:
                    leases.pop(root, None)
                else:
                    leases[root] = remaining

    def request_fleet_restart(self, workspace_root: str) -> str:
        """Restart the workspace's resident fleet so a new write-root grant takes effect (#1033).

        A workspace-shared fleet child is spawned once and keeps its compile-time write
        territory, so a mid-session ``POST /v1/workspaces/{wid}/grants`` for a new root would
        never reach it until it respawned — an over-claim on the grant's ``grant_applied_live``
        reason. This delegates to the drain-aware :meth:`WorkspaceExecutorReaper.request_restart`
        primitive (shared registry lock; never closes a busy/leased executor — it defers to the
        reaper's idle pass) and returns its TYPED outcome
        (:data:`RESTART_RESTARTED_LIVE` / :data:`RESTART_DEFERRED_BUSY` / :data:`RESTART_NO_RESIDENT`).
        The registry key is ``str(ws.root_path)`` — the same value the grant route derives — so
        an exact-string lookup matches the turn-bound executor.
        """

        from clio_agent.tools.reaper import RESTART_NO_RESIDENT  # noqa: PLC0415

        root = (workspace_root or "").strip()
        if not root:
            return RESTART_NO_RESIDENT
        # Ensure the shared (lock, executors, leases) exist even on a partially
        # constructed test stub; a missing reaper means no resident fleet to restart.
        self._workspace_state()
        reaper = getattr(self, "_workspace_reaper", None)
        if reaper is None:
            return RESTART_NO_RESIDENT
        return reaper.request_restart(root)

    def _discover_pack_experts(self) -> list[Any]:
        """Return loaded pack experts (for declared-tool visibility derivation)."""

        from clio_agent.gact.agent_blueprints import load_agent_blueprints  # noqa: PLC0415

        try:
            return list(load_agent_blueprints())
        except Exception as exc:  # noqa: BLE001 - best-effort
            if self.verbose:
                print(f"[ClioAgent] expert discovery failed: {exc}")
            return []

    def _run_chat_agent(
        self,
        question: str,
        session_context: str,
        *,
        images: list[Any] | None = None,
    ) -> str:
        """Generate a single-shot conversational reply through DSPy/LiteLLM.

        Plain LM synthesis over the chat signature — no tool loop. Reused by the
        session-compaction summarizer (routes/sessions.py) to fold a transcript
        into a compact summary.
        """
        self._raise_if_cancelled("chat_before")
        chat_context = self._chat_session_context(session_context)
        image_inputs = list(images or [])
        # P2.4: per-request BeforeModel/AfterModel wrapper (pure pass-through when no
        # model hook is configured, so this legacy chat/compaction path is unchanged).
        from clio_agent.lm.hooked_lm import wrap_lm_with_hooks  # noqa: PLC0415

        try:
            with dspy.context(lm=wrap_lm_with_hooks(self._main_lm), adapter=self._dspy_adapter):
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

    def _known_tool_names(self) -> set[str]:
        """Return every tool name currently visible to the executor."""
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

    def _parent_route_for_child(self, expert_id: str) -> str | None:
        """Return the public parent route for a child expert, if registered."""
        caps = self.registry.get_capabilities(expert_id)
        if caps is None or not caps.parent_id:
            return None
        return caps.parent_id

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

    def get_arc_stats(self) -> Dict[str, Any]:
        """Get ARC memory statistics."""
        return self.arc.get_cache_stats()

    def get_session_history(self, session_id: str, limit: int = 10) -> List[Conversation]:
        """Get conversation history for session from ARC Memory."""
        return self.arc.get_conversation_history(session_id, limit=limit)

    def shutdown(self) -> None:
        """Close the persistent MCP tool executors so their stdio children are reaped (#900)."""
        from clio_agent.runtime.process_tree import close_tool_executors

        reaper = getattr(self, "_workspace_reaper", None)
        if reaper is not None:
            reaper.stop()

        if self.verbose:
            print("[ClioAgent] Shutting down...")
        # Snapshot + clear under the shared registry lock: a reaper thread that
        # outlived stop()'s bounded join must not mutate the dict mid-iteration,
        # and clear() (not rebind) keeps every holder seeing the same registry.
        lock, workspace_executors, _leases = self._workspace_state()
        with lock:
            executors = [self.tool_executor, *workspace_executors.values()]
            workspace_executors.clear()
        close_tool_executors(executors)
