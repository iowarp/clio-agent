"""
ClioAgent - Main Agent Module

Agent-loop architecture over registered experts and tools.

Architecture:
    User Query -> Planner action
        -> tool call -> observation -> Planner action
        -> expert delegation -> expert result
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
import os
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import dspy

from clio_agent.arc.lsm import LSMTree
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.retrieval import ContextRetriever
from clio_agent.arc.schema import (
    Conversation,
    Invocation,
    Message,
    NanoagentSpawn,
    RoutingDecision,
)
from clio_agent.config import (
    create_chat_adapter,
    create_lm,
    create_planner_lm,
    fetch_lm_studio_models,
    has_explicit_model_override,
    load_config_from_env,
    select_models_for_agents,
)
from clio_agent.errors import (
    CancellationError,
    ClioError,
    ExpertError,
    ProviderError,
    RoutingError,
    ToolError,
)
from clio_agent.experts import AnalysisExpert, DataExpert, VisualizationExpert
from clio_agent.harness import (
    RouteDecision,
    RunTrace,
    compact_tool_result,
    extract_file_paths,
    normalize_tool_error,
    normalize_tool_result,
    tool_result_ok,
)
from clio_agent.optimizer.instrumentation import _extract_output
from clio_agent.registry.registry import AgentCapability, AgentRegistry
from clio_agent.signatures.main_agent_sig import (
    AgentActionSignature,
    AgentAnswerSignature,
    ChatAgentSignature,
)
from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError, validate_write_path
from clio_agent.tools.gateway import gateway

SCIENTIFIC_FILE_SUFFIXES = {".h5", ".hdf5", ".parquet", ".csv"}
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


DEFAULT_AGENT_MAX_STEPS = 4
ERROR_RECOVERY_ACTIONS = ("retry", "reconfigure_provider", "exit")


class ClioAgent(dspy.Module):
    """CLIO Agent with a planner loop over registered tools and experts.

    Architecture:
        User Query -> Planner action
            -> tool call -> observation -> next planner action
            -> expert delegation -> expert result
            -> answer from observations or conversation

    Attributes:
        action_planner: DSPy Predict module with AgentActionSignature
        chat_agent: DSPy Predict module with ChatAgentSignature
        data_expert: DataExpert instance with native HDF5 tools
        analysis_expert: AnalysisExpert instance with native Parquet/CSV tools
        visualization_expert: VisualizationExpert instance with matplotlib tools
        arc: ARC Memory instance
        context_retriever: Context retrieval module
        registry: Agent registry for discovery
        lsm: LSM Tree for metrics storage

    Example:
        >>> agent = ClioAgent()
        >>> result = agent(question="Optimize my HDF5 file", session_id="session-123")
        >>> print(result.answer)
        >>> print(result.selected_expert)  # "data", "analysis", "visualization", "chat", or "none"
    """

    def __init__(self, verbose: bool = False, data_dir: str = ".clio_agent"):
        """Initialize ClioAgent with planner, ChatAgent, and all experts.

        Args:
            verbose: If True, print reasoning and decisions
            data_dir: Base directory for ClioAgent data storage
        """
        super().__init__()
        self.verbose = verbose

        # Initialize ARC Memory
        self.arc = ARCMemory(data_dir=f"{data_dir}/arc", cache_capacity=1000)
        self.context_retriever = ContextRetriever(self.arc)

        # Initialize LSM Tree for metrics
        self.lsm = LSMTree(data_dir=f"{data_dir}/arc/lsm")

        # Initialize Agent Registry (for discovery, not routing)
        self.registry = AgentRegistry()

        # Load provider-agnostic config from environment
        self._provider_config = load_config_from_env()

        if self._provider_config.provider == "lm_studio" and not has_explicit_model_override():
            # LM Studio without an explicit model pin: discover loaded models
            # from the configured API base and use the same selected model for
            # planning and the global DSPy runtime.
            available_models = fetch_lm_studio_models(base_url=self._provider_config.api_base)
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
        self._main_lm = create_lm(self._provider_config)
        self._planner_lm = create_planner_lm(self._provider_config)
        self._router_lm = self._planner_lm  # Deprecated alias for older integrations.
        self._dspy_adapter = create_chat_adapter(self._provider_config)
        self.action_planner = dspy.Predict(AgentActionSignature)
        self.answer_synthesizer = dspy.Predict(AgentAnswerSignature)
        self.router = self.action_planner
        self._active_trace: RunTrace | None = None

        # Chat Agent: Predict for conversational responses. This keeps the
        # structured output surface smaller than ChainOfThought, which is more
        # reliable with local OpenAI-compatible backends.
        self.chat_agent = dspy.Predict(ChatAgentSignature)

        # Shared MCP executor: one explicit sync boundary for CLI/API thread calls.
        self.tool_executor = create_sync_tool_executor(gateway)

        # DataExpert: native deterministic HDF5 tools with optional DSPy synthesis
        self.data_expert = DataExpert(
            arc_memory=self.arc,
            tool_executor=self.tool_executor,
        )

        # AnalysisExpert: native deterministic Parquet/CSV tools with optional DSPy synthesis
        self.analysis_expert = AnalysisExpert(
            arc_memory=self.arc,
            tool_executor=self.tool_executor,
        )

        # VisualizationExpert: ReAct with matplotlib chart tools
        self.visualization_expert = VisualizationExpert(arc_memory=self.arc)

        # Register all experts in registry
        self.registry.register_agent(
            "data",
            self.data_expert,
            AgentCapability(
                keywords=["hdf5", "compression", "chunking", "data", "io"],
                description="Data I/O optimization expert with HDF5 tools",
                tools=[
                    "hdf5_list_datasets",
                    "hdf5_analyze_dataset",
                    "hdf5_check_compression",
                    "hdf5_optimize_chunking",
                    "hdf5_analyze_file",
                ],
                specialization="data_io",
                metadata={"file_suffixes": [".h5", ".hdf5"]},
            ),
        )

        self.registry.register_agent(
            "analysis",
            self.analysis_expert,
            AgentCapability(
                keywords=[
                    "parquet",
                    "statistics",
                    "schema",
                    "profiling",
                    "analysis",
                    "data quality",
                    "csv",
                ],
                description="Statistical analysis and data profiling expert with Parquet tools",
                tools=[
                    "parquet_analyze_schema",
                    "parquet_query_data",
                    "parquet_compute_statistics",
                    "csv_read_table",
                ],
                specialization="data_analysis",
                metadata={"file_suffixes": [".parquet", ".csv"]},
            ),
        )

        self.registry.register_agent(
            "visualization",
            self.visualization_expert,
            AgentCapability(
                keywords=["plot", "chart", "histogram", "scatter", "visualization", "graph"],
                description="Scientific data visualization expert with matplotlib tools",
                tools=[
                    "plot_histogram",
                    "plot_bar_chart",
                    "plot_scatter",
                    "plot_summary",
                ],
                specialization="data_visualization",
                metadata={"file_suffixes": [".parquet", ".csv"]},
            ),
        )

        # Load active variants for each expert (if any)
        try:
            from clio_agent.optimizer.variants import VariantManager

            vm = VariantManager(self.arc)
            for agent_id, expert_attr in [
                ("data", "data_expert"),
                ("analysis", "analysis_expert"),
                ("visualization", "visualization_expert"),
            ]:
                active = vm.get_active_variant(agent_id)
                if active and Path(active.file_path).exists():
                    try:
                        vm.load_variant(getattr(self, expert_attr), active.variant_id)
                        if self.verbose:
                            print(f"[ClioAgent] Loaded variant {active.variant_id} for {agent_id}")
                    except Exception as e:
                        if self.verbose:
                            print(
                                f"[ClioAgent] Warning: Could not load variant for {agent_id}: {e}"
                            )
        except Exception as e:
            if self.verbose:
                print(f"[ClioAgent] Warning: Variant loading failed: {e}")

        if self.verbose:
            print(f"[ClioAgent] Registered {self.registry.get_agent_count()} experts")
            print(f"[ClioAgent] ARC Memory initialized at {data_dir}/arc")
            print(f"[ClioAgent] LSM Tree initialized at {data_dir}/arc/lsm")

    def forward(
        self,
        question: str,
        session_id: str = "default",
        *,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dspy.Prediction:
        """Process a question through the CLIO agent loop.

        Flow:
            1. Retrieve session and current-file context from ARC
            2. Ask the planner for the next action using live capabilities
            3. Execute tools or experts and append observations
            4. Answer from observations or direct conversation
            5. Store decisions, provenance, metrics, and conversation in ARC

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
            duration_ms, arc_stats, lsm_stats
        """
        if cancel_requested is not None:
            with cancellation_checker(cancel_requested):
                return self.forward(
                    question,
                    session_id=session_id,
                    session_mode=session_mode,
                    session_edit_mode=session_edit_mode,
                )

        start_time = time.time()

        # Step 1: Retrieve context from ARC Memory
        session_context = self._get_session_context(question, session_id)
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
        self._active_trace = trace

        if self.verbose:
            print(f"[Planner] {question[:50]}...")

        success = False
        error_msg = None
        selected = "chat"
        answer = ""
        expert_result = None
        error_info = None

        try:
            selected, answer, expert_result, error_info, route = self._run_agent_loop(
                question=question,
                session_context=session_context,
                file_context=file_context,
                trace=trace,
                routing_mode=routing_mode,
            )
            trace.route = route
            success = True
            if selected in ("data", "analysis", "visualization") and error_info is None:
                error_info = self._tool_error_info_from_trace(selected, trace)
            if error_info and not error_info.get("details", {}).get("partial", False):
                success = False
                error_msg = str(error_info.get("message") or "Tool execution failed.")
                answer = ""
        except Exception as e:
            success = False
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
        if selected in ("data", "analysis", "visualization"):
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
        self._active_trace = None

        return dspy.Prediction(
            answer=answer,
            selected_expert=selected,
            tools_called=[tool.to_arc_tool_call() for tool in trace.tools],
            file_diffs=self._file_diffs_from_trace(
                trace,
                edit_mode=session_edit_mode,
            ),
            route_source=route.source,
            route_reason=route.reason,
            session_id=session_id,
            duration_ms=duration_ms,
            arc_stats=self.arc.get_cache_stats(),
            lsm_stats=self.lsm.get_stats(),
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
        routing_mode: str = "auto",
    ) -> tuple[str, str, Any, dict[str, Any] | None, RouteDecision]:
        """Run the planner/executor loop for one user request."""
        self._raise_if_cancelled("agent_loop_start")
        if routing_mode == "chat":
            answer = self._run_chat_agent(question, session_context)
            route = self._route_for_selected(
                "chat",
                "Session routing_mode='chat' forced the conversational path.",
                confidence=1.0,
            )
            return "chat", answer, None, None, route

        capabilities = self._build_capabilities_context(routing_mode=routing_mode)
        observations: list[dict[str, Any]] = []
        selected = "chat"
        route = trace.route

        for step in range(self._agent_max_steps()):
            self._raise_if_cancelled("planner_before")
            action = self._plan_next_action(
                question=question,
                session_context=session_context,
                file_context=file_context,
                capabilities=capabilities,
                observations=observations,
            )
            self._raise_if_cancelled("planner_after")
            kind = self._coerce_text(action.get("action")).strip().lower()
            reason = self._coerce_text(action.get("reason")).strip()

            if kind == "tool":
                tool_name = self._coerce_text(action.get("tool")).strip()
                selected = self._selected_expert_for_tool(tool_name)
                self._raise_if_cancelled("tool_before")
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

            if kind == "expert":
                expert_id = self._coerce_text(action.get("expert")).strip().lower()
                expert_question = self._coerce_text(action.get("question")).strip() or question
                if self._should_answer_with_chat(question, file_context):
                    if routing_mode == "experts":
                        raise RoutingError(
                            "Session routing_mode='experts' requires an expert/tool action, "
                            "but the planner selected an expert without concrete file or data "
                            "context.",
                            details=self._recovery_details(
                                requested_mode=routing_mode,
                                planner_action=action,
                            ),
                        )
                    answer = self._run_chat_agent(question, session_context)
                    route = self._route_for_selected(
                        "chat",
                        "Planner expert action ignored because no concrete file/data context exists.",
                        confidence=0.65,
                    )
                    return "chat", answer, None, None, route
                compatibility_error = self._expert_file_compatibility_error(
                    expert_id,
                    file_context,
                )
                if compatibility_error is not None:
                    observations.append(
                        {
                            "step": step + 1,
                            "type": "planner_error",
                            "ok": False,
                            "result": compatibility_error,
                        }
                    )
                    continue
                self._raise_if_cancelled("expert_before")
                selected, answer, expert_result, error_info = self._dispatch_expert_action(
                    expert_id=expert_id,
                    question=expert_question,
                    file_context=file_context,
                    trace=trace,
                )
                self._raise_if_cancelled("expert_after")
                route = self._route_for_selected(
                    selected,
                    reason or f"Agent planner delegated to the {selected} expert.",
                    confidence=0.75,
                )
                return selected, answer, expert_result, error_info, route

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
                if self._should_replace_planner_text(
                    kind=kind,
                    question=question,
                    session_context=session_context,
                    answer=answer,
                ):
                    raise RoutingError(
                        "Agent planner selected no action with stale or in-scope answer text.",
                        details=self._recovery_details(
                            planner_action=action,
                            replacement_reason="stale_or_in_scope_text",
                        ),
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
                if self._should_replace_planner_text(
                    kind=kind,
                    question=question,
                    session_context=session_context,
                    answer=answer,
                ):
                    if not observations:
                        raise RoutingError(
                            "Agent planner produced stale or invalid direct answer text.",
                            details=self._recovery_details(
                                planner_action=action,
                                replacement_reason="stale_or_invalid_answer_text",
                            ),
                        )
                    answer = ""
                if not answer and observations:
                    answer = self._synthesize_agent_answer(
                        question=question,
                        session_context=session_context,
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
            observations=observations,
        )
        self._raise_if_cancelled("answer_synthesis_after")
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
                step_limit=self._agent_max_steps(),
                planner_observations=observations[-3:],
            ),
        ).to_dict()
        return selected, answer, None, error_info, route

    def _plan_next_action(
        self,
        *,
        question: str,
        session_context: str,
        file_context: str,
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
        try:
            with dspy.context(lm=self._planner_lm, adapter=self._dspy_adapter):
                result = self.action_planner(
                    question=question,
                    session_context=session_context,
                    file_context=file_context or "No current file context",
                    capabilities=capabilities,
                    observations=observations_text,
                )
            return self._parse_action_json(getattr(result, "action_json", ""))
        except Exception as planner_error:
            raw_action = self._parse_action_from_adapter_error(planner_error)
            if raw_action is not None:
                return raw_action
            if self.verbose:
                print(f"[Planner] DSPy planner failed: {planner_error}")
            raise RoutingError(
                "Agent planner failed to produce an action.",
                details={"original_error": str(planner_error)},
            ) from planner_error

    def _dispatch_expert_action(
        self,
        *,
        expert_id: str,
        question: str,
        file_context: str,
        trace: RunTrace,
    ) -> tuple[str, str, Any, dict[str, Any] | None]:
        """Execute one planner-selected expert delegation."""
        if expert_id not in self.registry.list_agents():
            error = ExpertError(
                f"Unknown expert selected by planner: {expert_id}",
                details=self._recovery_details(
                    expert=expert_id,
                    available=self.registry.list_agents(),
                ),
            ).to_dict()
            return "none", "", None, error

        expert_question = self._question_with_file_context(question, file_context)
        try:
            with dspy.context(lm=self._main_lm, adapter=self._dspy_adapter):
                if expert_id == "data":
                    expert_result = self.data_expert(
                        question=expert_question, file_context=file_context
                    )
                    self._merge_expert_provenance(trace, expert_result)
                    answer = (
                        f"{expert_result.analysis}\n\n"
                        f"Recommendations:\n{expert_result.recommendations}"
                    )
                    return "data", answer, expert_result, None

                if expert_id == "analysis":
                    expert_result = self.analysis_expert(
                        question=expert_question,
                        file_context=file_context,
                    )
                    self._merge_expert_provenance(trace, expert_result)
                    answer = (
                        f"{expert_result.analysis}\n\n"
                        f"Recommendations:\n{expert_result.recommendations}"
                    )
                    return "analysis", answer, expert_result, None

                expert_result = self.visualization_expert(
                    question=expert_question,
                    file_context=file_context,
                )
            description = self._coerce_text(
                getattr(expert_result, "visualization_description", "")
            ).strip()
            file_path = self._coerce_text(getattr(expert_result, "file_path", "")).strip()
            answer = f"Visualization: {description}\n\nFile: {file_path}".strip()
            return (
                "visualization",
                answer,
                expert_result,
                getattr(expert_result, "error_info", None),
            )
        except CancellationError:
            raise
        except Exception as exc:
            error = ExpertError(
                f"The {expert_id} expert encountered an issue processing your request.",
                details=self._recovery_details(
                    expert=expert_id,
                    original_error=str(exc),
                ),
            ).to_dict()
            return expert_id, "", None, error

    def _execute_tool_action(
        self,
        tool_name: str,
        raw_args: Any,
        trace: RunTrace,
    ) -> Any:
        """Execute a planner-selected tool and record provenance."""
        args = self._normalize_tool_args(raw_args)
        visualization_tools = self._visualization_tool_map()
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

        if tool_name in visualization_tools:
            return self._execute_visualization_tool(tool_name, visualization_tools[tool_name], args)

        start = time.time()
        try:
            raw_result = self.tool_executor.call_tool(tool_name, args)
            result = normalize_tool_result(self._decode_tool_result(raw_result), tool=tool_name)
        except CancellationError:
            raise
        except Exception as exc:
            result = {"error": normalize_tool_error(exc, tool=tool_name, code="tool_exception")}
        duration_ms = (time.time() - start) * 1000
        trace.record_tool(
            tool=tool_name,
            params=args,
            result=result,
            duration_ms=duration_ms,
            ok=tool_result_ok(result),
        )
        return result

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

    def _execute_visualization_tool(self, tool_name: str, tool: Any, args: dict[str, Any]) -> Any:
        """Execute one local visualization tool with policy-aware artifact defaults."""
        args = dict(args)
        filepath = self._coerce_text(args.get("filepath")).strip()
        if filepath and not self._coerce_text(args.get("output_path")).strip():
            prepared = self._prepare_visualization_output_path(tool_name, filepath)
            if isinstance(prepared, dict) and "error" in prepared:
                start = time.time()
                self._record_tool_call(tool_name, args, prepared, (time.time() - start) * 1000)
                return prepared
            args["output_path"] = str(prepared)

        start = time.time()
        try:
            result = normalize_tool_result(
                self._call_tool_function(tool, **args),
                tool=tool_name,
            )
        except CancellationError:
            raise
        except Exception as exc:
            result = {"error": normalize_tool_error(exc, tool=tool_name, code="tool_exception")}
        duration_ms = (time.time() - start) * 1000
        self._record_tool_call(tool_name, args, result, duration_ms)
        return result

    def _prepare_visualization_output_path(
        self, tool_name: str, filepath: str
    ) -> Path | dict[str, Any]:
        """Return a safe default chart output path or a normalized policy error."""
        source_path = Path(filepath).expanduser()
        artifact_root = self._default_artifact_root(source_path)
        output_dir = artifact_root / "charts"
        default_name = f"{tool_name.removeprefix('plot_')}_{source_path.stem}.png"
        output_path = output_dir / default_name
        try:
            validate_write_path(str(artifact_root.parent / f".{artifact_root.name}.probe"))
            output_dir.mkdir(parents=True, exist_ok=True)
            return validate_write_path(str(output_path))
        except FilePolicyError as exc:
            return {"error": normalize_tool_error(exc.to_result()["error"], tool=tool_name)}

    def _synthesize_agent_answer(
        self,
        *,
        question: str,
        session_context: str,
        observations: list[dict[str, Any]],
    ) -> str:
        """Produce a final answer from observations or surface synthesis failure."""
        self._raise_if_cancelled("answer_synthesis_before")
        observations_text = self._format_observations_for_prompt(observations)
        try:
            with dspy.context(lm=self._main_lm, adapter=self._dspy_adapter):
                result = self.answer_synthesizer(
                    question=question,
                    session_context=session_context,
                    observations=observations_text,
                )
            answer = self._coerce_text(getattr(result, "answer", "")).strip()
            self._raise_if_cancelled("answer_synthesis_after")
            if answer:
                return answer
        except CancellationError:
            raise
        except Exception as exc:
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
        raise ProviderError(
            "CLIO could not synthesize a final answer from the completed observations.",
            details=self._recovery_details(
                stage="answer_synthesis",
                original_error="answer synthesizer returned an empty answer",
                observations=observations[-3:],
            ),
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
        mode = str(_ROUTING_MODE_OVERRIDE.get() or "").strip().lower()
        if not mode:
            mode = str(getattr(self, "_routing_mode_override", "auto") or "auto").strip().lower()
        if mode in {"auto", "chat", "experts", "reasoning_only"}:
            return mode
        return "auto"

    def _build_capabilities_context(self, routing_mode: str = "auto") -> str:
        """Describe live experts and tools for the planner without query heuristics."""
        lines = ["Experts:"]
        for agent_id in self.registry.list_agents():
            caps = self.registry.get_capabilities(agent_id)
            if caps is None:
                continue
            tools = ", ".join(caps.tools) if caps.tools else "no direct tools"
            suffixes = ", ".join(caps.metadata.get("file_suffixes", []))
            file_note = f"; files: {suffixes}" if suffixes else ""
            lines.append(f"- {agent_id}: {caps.description}{file_note}; tools: {tools}")

        lines.append("Tools:")
        for tool in sorted(self._available_dspy_tools(), key=lambda t: t.name):
            arg_names = ", ".join(sorted((getattr(tool, "args", {}) or {}).keys()))
            desc = self._first_sentence(self._coerce_text(getattr(tool, "desc", "")))
            if arg_names:
                lines.append(f"- {tool.name}({arg_names}): {desc}")
            else:
                lines.append(f"- {tool.name}: {desc}")
        if routing_mode == "experts":
            lines.append(
                "Routing override: experts mode is active. Do not choose answer or none "
                "before a tool or expert has produced an observation."
            )
        elif routing_mode == "reasoning_only":
            lines.append(
                "Routing override: reasoning_only mode is active. Prefer the planner's "
                "tool/expert reasoning path over deterministic shortcuts."
            )
        return "\n".join(lines)

    def _available_dspy_tools(self) -> list[dspy.Tool]:
        """Return gateway and local visualization tools visible to the planner."""
        return [*self.tool_executor.to_dspy_tools(), *self._visualization_tool_map().values()]

    def _known_tool_names(self) -> set[str]:
        """Return every tool name currently visible to the planner."""
        return set(self.tool_executor.get_tool_names()) | set(self._visualization_tool_map())

    def _visualization_tool_map(self) -> dict[str, dspy.Tool]:
        """Return local visualization tools keyed by their stable names."""
        return {
            tool.name: tool
            for tool in getattr(self.visualization_expert, "_tools", [])
            if hasattr(tool, "name")
        }

    def _selected_expert_for_tool(self, tool_name: str) -> str:
        """Resolve a tool's owning expert from the registered capability table."""
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
                return selected
        return "chat"

    def _expert_file_compatibility_error(
        self,
        expert_id: str,
        file_context: str,
    ) -> dict[str, Any] | None:
        """Reject expert delegation that cannot inspect the current file context."""
        paths = extract_file_paths(file_context, "", SCIENTIFIC_FILE_SUFFIXES)
        if not paths:
            return None

        caps = self.registry.get_capabilities(expert_id)
        if caps is None:
            return {
                "message": f"Unknown expert {expert_id!r}.",
                "available_experts": self.registry.list_agents(),
            }

        supported = {
            str(suffix).lower()
            for suffix in caps.metadata.get("file_suffixes", [])
            if str(suffix).strip()
        }
        if not supported:
            return None

        unsupported = [str(path) for path in paths if path.suffix.lower() not in supported]
        if not unsupported:
            return None

        compatible = self._compatible_experts_for_paths(paths)
        return {
            "message": (
                f"Expert {expert_id!r} cannot inspect the current file context "
                f"({', '.join(unsupported)}). Choose a compatible expert or tool."
            ),
            "expert": expert_id,
            "supported_suffixes": sorted(supported),
            "compatible_experts": compatible,
        }

    def _compatible_experts_for_paths(self, paths: list[Path]) -> list[str]:
        """List registered experts that support all current file suffixes."""
        suffixes = {path.suffix.lower() for path in paths}
        compatible: list[str] = []
        for agent_id in self.registry.list_agents():
            caps = self.registry.get_capabilities(agent_id)
            if caps is None:
                continue
            supported = {
                str(suffix).lower()
                for suffix in caps.metadata.get("file_suffixes", [])
                if str(suffix).strip()
            }
            if supported and suffixes.issubset(supported):
                compatible.append(agent_id)
        return compatible

    @staticmethod
    def _route_for_selected(selected: str, reason: str, confidence: float) -> RouteDecision:
        """Build the public route decision for a planner-selected handler."""
        valid_targets = {"chat", "data", "analysis", "visualization", "none"}
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
                raise ValueError(f"Planner returned invalid JSON action: {raw!r}")
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                try:
                    decoded, end = json.JSONDecoder().raw_decode(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Planner returned invalid JSON action: {raw!r}") from exc
                trailing = text[end:].strip()
                # DSPy ChatAdapter error strings can append one bracket after the LM payload.
                if trailing != "]":
                    raise ValueError(f"Planner returned invalid JSON action: {raw!r}") from None
            if not isinstance(decoded, dict):
                raise ValueError(f"Planner action must be a JSON object: {raw!r}")

        action = cls._coerce_text(decoded.get("action")).strip().lower()
        if action not in {"tool", "expert", "answer", "none"}:
            raise ValueError(f"Planner returned unsupported action: {decoded!r}")
        decoded["action"] = action
        return decoded

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
        except ValueError:
            return None

    def _format_observations_for_prompt(self, observations: list[dict[str, Any]]) -> str:
        """Format loop observations as compact JSON for planner prompts."""
        if not observations:
            return "No observations yet"
        return json.dumps(observations, ensure_ascii=False, indent=2)

    @classmethod
    def _should_answer_with_chat(cls, question: str, file_context: str) -> bool:
        """Return whether an expert action should be kept in chat.

        Without a concrete file path or current file context, broad capability
        and workflow questions should not be sent to data experts. Weak local
        planners otherwise produce plausible but file-context-dependent expert
        answers for ordinary conversation.
        """
        if file_context.strip() or extract_file_paths(question, "", SCIENTIFIC_FILE_SUFFIXES):
            return False

        lowered = " ".join(question.lower().split())
        general_prefixes = (
            "briefly",
            "explain",
            "how ",
            "if ",
            "summarize",
            "tell me",
            "what ",
            "when ",
            "why ",
        )
        general_terms = (
            "capabilit",
            "can you do",
            "local data file",
            "previous answer",
            "provider",
            "safe next step",
            "workflow",
        )
        return lowered.startswith(general_prefixes) or any(
            term in lowered for term in general_terms
        )

    @classmethod
    def _should_replace_planner_text(
        cls,
        *,
        kind: str,
        question: str,
        session_context: str,
        answer: str,
    ) -> bool:
        """Return whether planner text is stale or invalid for the route."""
        if not answer:
            return False

        lowered = answer.lower()
        if "file_context" in lowered or "no current file context" in lowered:
            return True

        if kind == "none" and not cls._question_looks_out_of_scope(question):
            return True

        previous = cls._last_assistant_context(session_context)
        return cls._text_similarity(answer, previous) >= 0.72

    @staticmethod
    def _question_looks_out_of_scope(question: str) -> bool:
        """Return whether a request is clearly outside CLIO's domain."""
        lowered = question.lower()
        in_scope_terms = (
            "analysis",
            "clio",
            "data",
            "file",
            "hdf5",
            "parquet",
            "previous answer",
            "provider",
            "scientific",
            "summarize",
            "visual",
        )
        return not any(term in lowered for term in in_scope_terms)

    @staticmethod
    def _last_assistant_context(session_context: str) -> str:
        """Extract the most recent assistant line from compiled context."""
        for line in reversed(session_context.splitlines()):
            if line.lower().startswith("assistant:"):
                return line.split(":", 1)[1].strip()
        return ""

    @staticmethod
    def _assistant_context_lines(session_context: str) -> list[str]:
        """Extract assistant lines from compiled context."""
        lines: list[str] = []
        for line in session_context.splitlines():
            if line.lower().startswith("assistant:"):
                text = line.split(":", 1)[1].strip()
                if text:
                    lines.append(text)
        return lines

    @classmethod
    def _summarize_assistant_context(cls, session_context: str) -> str:
        """Build a short deterministic summary from prior assistant turns."""
        snippets: list[str] = []
        for line in cls._assistant_context_lines(session_context):
            snippet = cls._first_sentence(line, max_chars=120).strip()
            if not snippet:
                continue
            if any(cls._text_similarity(snippet, existing) >= 0.7 for existing in snippets):
                continue
            snippets.append(snippet)
            if len(snippets) >= 4:
                break

        if not snippets:
            return ""
        return "Previous answers covered: " + "; ".join(snippets) + "."

    @staticmethod
    def _question_requests_summary(question: str) -> bool:
        """Return whether the user is asking to summarize prior answers."""
        lowered = question.lower()
        return "summar" in lowered and (
            "previous" in lowered or "prior" in lowered or "earlier" in lowered
        )

    @staticmethod
    def _text_similarity(left: str, right: str) -> float:
        """Small token-overlap score for repeated context detection."""
        if not left or not right:
            return 0.0
        left_tokens = {token for token in left.lower().split() if len(token) > 3}
        right_tokens = {token for token in right.lower().split() if len(token) > 3}
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

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
        raw = os.environ.get("CLIO_AGENT_MAX_STEPS", str(DEFAULT_AGENT_MAX_STEPS))
        try:
            value = int(raw)
        except ValueError:
            value = DEFAULT_AGENT_MAX_STEPS
        return max(1, min(value, 12))

    @staticmethod
    def _merge_expert_provenance(trace: RunTrace, expert_result: Any) -> None:
        """Copy native expert tool provenance into the active run trace."""
        provenance = getattr(expert_result, "tool_provenance", None)
        if not isinstance(provenance, (list, tuple)):
            return
        for observation in provenance:
            if hasattr(observation, "to_arc_tool_call"):
                trace.tools.append(observation)

    def _run_chat_agent(self, question: str, session_context: str) -> str:
        """Generate a conversational reply through DSPy/LiteLLM."""
        self._raise_if_cancelled("chat_before")
        try:
            with dspy.context(lm=self._main_lm, adapter=self._dspy_adapter):
                result = self.chat_agent(question=question, session_context=session_context)
            answer = self._coerce_text(getattr(result, "answer", None)).strip()
            self._raise_if_cancelled("chat_after")
            if answer:
                if self._question_requests_summary(question):
                    summary = self._summarize_assistant_context(session_context)
                    if summary:
                        return summary
                return answer
            raise ValueError("Chat agent returned an empty answer.")
        except Exception as chat_error:
            if self.verbose:
                print(f"[ClioAgent] ChatAgent failed: {chat_error}")
            raise

    @staticmethod
    def _default_artifact_root(filepath: Path) -> Path:
        """Return an artifact root that respects configured file policy roots."""
        configured = os.environ.get("CLIO_ARTIFACT_DIR", "").strip()
        if configured:
            return Path(configured).expanduser()

        if not os.environ.get("CLIO_ALLOWED_ROOTS", "").strip():
            return Path("/tmp/clio-agent-artifacts")

        policy = FileAccessPolicy.from_env()
        resolved_file = filepath.expanduser().resolve(strict=False)
        for root in policy.allowed_roots:
            try:
                resolved_file.relative_to(root)
                return root / ".clio-agent-artifacts"
            except ValueError:
                continue
        return policy.allowed_roots[0] / ".clio-agent-artifacts"

    @classmethod
    def _tool_error_info_from_trace(
        cls,
        selected: str,
        trace: RunTrace,
    ) -> dict[str, Any] | None:
        """Return structured error_info for the first failed tool in a trace."""
        successful_tools = [tool.tool for tool in trace.tools if tool.ok]
        for observation in trace.tools:
            if observation.ok:
                continue
            error = cls._tool_error_from_result(observation.tool, observation.result)
            info = cls._tool_error_info(
                selected=selected,
                tool=observation.tool,
                error=error,
                partial=bool(successful_tools),
            )
            if successful_tools:
                info["details"]["successful_tools"] = successful_tools
            return info
        return None

    @staticmethod
    def _tool_error_from_result(tool: str, result: Any) -> dict[str, Any]:
        """Extract one normalized tool error from a raw or structured result."""
        normalized = normalize_tool_result(result, tool=tool)
        if isinstance(normalized, dict) and "error" in normalized:
            return normalize_tool_error(normalized["error"], tool=tool)
        return normalize_tool_error(result, tool=tool)

    @staticmethod
    def _tool_error_info(
        *,
        selected: str,
        tool: str,
        error: dict[str, Any],
        partial: bool,
    ) -> dict[str, Any]:
        """Build the public result error_info shape for handled tool failures."""
        normalized = normalize_tool_error(error, tool=tool)
        message = str(normalized.get("message") or f"{tool} failed.")
        return ToolError(
            message,
            details={
                "expert": selected,
                "tool": tool,
                "tool_error": normalized,
                "partial": partial,
                "recovery_actions": list(ERROR_RECOVERY_ACTIONS),
            },
        ).to_dict()

    @staticmethod
    def _call_tool_function(tool: Any, *args: Any, **kwargs: Any) -> Any:
        """Call either a FastMCP FunctionTool or a plain Python helper."""
        fn = getattr(tool, "fn", None) or getattr(tool, "func", None) or tool
        return fn(*args, **kwargs)

    def _run_local_tool(self, name: str, tool: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a local tool and record its result in the active harness trace."""
        start = time.time()
        result = self._call_tool_function(tool, *args, **kwargs)
        duration_ms = (time.time() - start) * 1000
        params = self._bind_tool_params(tool, args, kwargs)
        self._record_tool_call(name, params, result, duration_ms)
        return result

    def _record_tool_call(
        self,
        name: str,
        params: dict[str, Any],
        result: Any,
        duration_ms: float,
    ) -> None:
        """Record a tool call if a run trace is active."""
        if self._active_trace is None:
            return
        self._active_trace.record_tool(
            tool=name,
            params=params,
            result=result,
            duration_ms=duration_ms,
            ok=tool_result_ok(result),
        )

    @staticmethod
    def _bind_tool_params(
        tool: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        """Best-effort conversion of positional tool args into named params."""
        import inspect

        fn = getattr(tool, "fn", None) or getattr(tool, "func", None) or tool
        try:
            bound = inspect.signature(fn).bind_partial(*args, **kwargs)
            return dict(bound.arguments)
        except Exception:
            params: dict[str, Any] = {"args": list(args)}
            params.update(kwargs)
            return params

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
            except Exception:
                return str(value)

        # Pydantic v2 models: avoid warning-emitting serialization paths.
        if hasattr(value, "model_dump"):
            try:
                dumped = value.model_dump(mode="json", warnings="none")
                return json.dumps(dumped, ensure_ascii=False)
            except Exception:
                pass

        # Common chat/message object shape from LM backends.
        content = getattr(value, "content", None)
        if isinstance(content, str):
            return content

        return str(value)

    def _get_session_context(self, question: str, session_id: str, tier: int = 2) -> str:
        """Retrieve compiled session context from ARC Memory.

        Uses ContextCompiler pipeline (filter -> compact -> enrich -> assemble)
        with token budgets per tier. Falls back to "No prior context" on error.

        Args:
            question: User's current question
            session_id: Session identifier
            tier: Agent tier for token budget (1=planner/2K, 2=expert/4K)

        Returns:
            Compiled context string or "No prior context"
        """
        try:
            compiled = self.context_retriever.compile_expert_context(
                query=question,
                session_id=session_id,
                tier=tier,
            )
            if self.verbose:
                print(f"[ClioAgent] Compiled context ({len(compiled)} chars, tier={tier})")
            return compiled
        except Exception as e:
            if self.verbose:
                print(f"[ClioAgent] Warning: ContextCompiler failed: {e}, falling back")
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
            except Exception:
                pass
        return "No prior context"

    def _get_file_context(self, session_id: str, active_file: Path | None = None) -> str:
        """Load dataset profiles from ARC for expert file context.

        Args:
            session_id: Session identifier

        Returns:
            JSON string of dataset profiles, or empty string if none.
        """
        try:
            profiles = self.arc.get_session_profiles(session_id)
            if profiles:
                context = json.dumps(
                    [
                        {
                            "filepath": p.filepath,
                            "schema": p.schema_info,
                            "stats": p.statistics,
                        }
                        for p in profiles
                    ]
                )
                if active_file is not None:
                    return f"{context}\nCurrent session file: {active_file}"
                return context
        except Exception:
            pass
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
        try:
            conv = self.arc.get_conversation(session_id)
        except Exception:
            return None
        if conv is None:
            return None

        for message in reversed(conv.messages):
            paths = extract_file_paths(message.content, "", SCIENTIFIC_FILE_SUFFIXES)
            if paths:
                return paths[0]
        return None

    @staticmethod
    def _question_with_session_file(question: str, active_file: Path | None) -> str:
        """Append current file context so native tools own the facts."""
        if active_file is None:
            return question
        if extract_file_paths(question, "", SCIENTIFIC_FILE_SUFFIXES):
            return question
        return f"{question}\n\nUse this file from the current session: {active_file}"

    @staticmethod
    def _question_with_file_context(question: str, file_context: str) -> str:
        """Append current file context when a planner expert action omits the path."""
        if extract_file_paths(question, "", SCIENTIFIC_FILE_SUFFIXES):
            return question
        paths = extract_file_paths(file_context, "", SCIENTIFIC_FILE_SUFFIXES)
        if not paths:
            return question
        return f"{question}\n\nUse this file from the current session: {paths[0]}"

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
        except Exception as e:
            if self.verbose:
                print(f"[ClioAgent] Warning: Failed to store routing decision: {e}")

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
        """Store invocation metrics in LSM Tree and ARC Memory.

        Args:
            question: User's question
            session_id: Session identifier
            selected_expert: Which expert handled the query
            duration_ms: Processing duration in milliseconds
            success: Whether the query succeeded
            error_msg: Error message if failed
        """
        # Write to LSM Tree
        self.lsm.write(
            timestamp=time.time(),
            metric={
                "session_id": session_id,
                "query": question,
                "selected_expert": selected_expert,
                "duration_ms": duration_ms,
                "success": success,
                "error": error_msg,
            },
        )

        # Store invocation in ARC Memory
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
        except Exception as e:
            if self.verbose:
                print(f"[ClioAgent] Warning: Failed to store expert invocation: {e}")

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
                metadata={"clio_agent_version": "0.2.0", "arc_enabled": True},
                storage_tier="warm",
            )
            self.arc.store_conversation(conv)

    def get_arc_stats(self) -> Dict[str, Any]:
        """Get ARC memory statistics."""
        return self.arc.get_cache_stats()

    def get_lsm_stats(self) -> Dict[str, Any]:
        """Get LSM Tree statistics."""
        return self.lsm.get_stats()

    def get_session_history(self, session_id: str, limit: int = 10) -> List[Conversation]:
        """Get conversation history for session from ARC Memory."""
        return self.arc.get_conversation_history(session_id, limit=limit)

    def shutdown(self) -> None:
        """Clean shutdown of ClioAgent resources."""
        if self.verbose:
            print("[ClioAgent] Shutting down...")

        for attr in ("data_expert", "analysis_expert", "visualization_expert"):
            expert = getattr(self, attr, None)
            close = getattr(expert, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:
                    if self.verbose:
                        print(f"[ClioAgent] Warning: failed to close {attr}: {e}")

        self.lsm.close()

        if self.verbose:
            print("[ClioAgent] LSM Tree closed")
            print("[ClioAgent] Shutdown complete")


def load_optimized_clio_agent(path: str, verbose: bool = False) -> ClioAgent:
    """Load an optimized ClioAgent agent from disk.

    Args:
        path: Path to saved ClioAgent JSON
        verbose: If True, print loading info

    Returns:
        Optimized ClioAgent instance
    """
    raise NotImplementedError("Optimization loading not yet implemented")
