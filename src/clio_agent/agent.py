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
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Literal

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
from clio_agent.experts.ndp_expert import NDPExpert
from clio_agent.experts.sac_format_expert import SAC_SUFFIXES, SACFormatExpert
from clio_agent.harness import (
    SPECIAL_ROUTE_TARGETS,
    RouteDecision,
    RunTrace,
    ToolObservation,
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
from clio_agent.tools.catalog import tool_owner, tool_tags, tool_visible_to
from clio_agent.tools.execution import create_sync_tool_executor, notify_global_tool_observer
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError, validate_write_path
from clio_agent.tools.gateway import gateway

SCIENTIFIC_FILE_SUFFIXES = {
    ".h5",
    ".hdf5",
    ".parquet",
    ".csv",
    ".bp",
    ".bp4",
    ".bp5",
    ".sac",
    ".tar",
    ".tgz",
    ".gz",
    ".fa",
    ".fasta",
    ".fna",
    ".vcf",
    ".cif",
    ".geojson",
    ".png",
    ".mzml",
}
PLANNER_HIDDEN_TOOL_NAMES = {"fs_read_file", "fs_apply_edit_write"}


def _mass_spec_qc_sentence(result: Any) -> str:
    """Return readable mass-spec QC wording from an mzML tool result."""
    if not isinstance(result, Mapping):
        return ""
    ms_levels = result.get("ms_levels")
    ms_level_text = json.dumps(ms_levels, sort_keys=True) if isinstance(ms_levels, Mapping) else "{}"
    tic_total = result.get("total_ion_current_total", result.get("tic_total", 0))
    tic_max = result.get("total_ion_current_max", result.get("tic_max", 0))
    return (
        f"MS level distribution: {ms_level_text}. "
        f"Total ion current evidence: total={tic_total}, max={tic_max}."
    )
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
        self.ndp_catalog_expert = NDPExpert(tool_executor=self.tool_executor)
        self.sac_format_expert = SACFormatExpert(tool_executor=self.tool_executor)

        # VisualizationExpert: ReAct with matplotlib chart tools
        self.visualization_expert = VisualizationExpert(arc_memory=self.arc)

        # Register all experts in registry
        self.registry.register_agent(
            "data",
            self.data_expert,
            AgentCapability(
                keywords=[
                    "hdf5",
                    "adios",
                    "bp5",
                    "compression",
                    "chunking",
                    "data",
                    "io",
                    "catalog",
                ],
                description=(
                    "Data acquisition, discovery, staging, and I/O manager. Owns the "
                    "first phase for finding, ranking, downloading, or staging external "
                    "datasets before analysis or visualization. Delegates external "
                    "catalogs and data-format discovery to nested data experts."
                ),
                tools=[
                    "hdf5_list_datasets",
                    "hdf5_analyze_dataset",
                    "hdf5_check_compression",
                    "hdf5_optimize_chunking",
                    "hdf5_analyze_file",
                    "adios_inspect_file",
                    "adios_inspect_variables",
                    "adios_inspect_profiling",
                ],
                specialization="data_io",
                metadata={
                    "file_suffixes": [".h5", ".hdf5", ".bp", ".bp4", ".bp5"],
                    "delegates_to": ["ndp_catalog", "sac_format"],
                },
            ),
        )

        self.registry.register_agent(
            "ndp_catalog",
            self.ndp_catalog_expert,
            AgentCapability(
                keywords=[
                    "ndp",
                    "national data platform",
                    "earthscope",
                    "catalog",
                    "dataset discovery",
                    "resource",
                    "staging",
                ],
                description=(
                    "Nested data expert for National Data Platform and EarthScope-style "
                    "dataset discovery, metadata inspection, resource ranking, and "
                    "bounded staging."
                ),
                tools=[
                    "ndp_list_organizations",
                    "ndp_search_datasets",
                    "ndp_get_dataset_details",
                    "ndp_stage_resource",
                ],
                specialization="data_catalog",
                parent_id="data",
                source="builtin_nested",
                metadata={
                    "provider": "ndp",
                    "future_model_boundary": True,
                },
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
                description=(
                    "Statistical analysis, data profiling, data quality triage, and "
                    "cross-file scientific review expert for already available local "
                    "data. Does not own external dataset discovery or staging; those "
                    "phases belong to data first."
                ),
                tools=[
                    "parquet_analyze_schema",
                    "parquet_query_data",
                    "parquet_compute_statistics",
                    "csv_read_table",
                ],
                specialization="data_analysis",
                metadata={
                    "file_suffixes": [".parquet", ".csv"],
                    "coordinated_file_suffixes": [
                        ".h5",
                        ".hdf5",
                        ".bp",
                        ".bp4",
                        ".bp5",
                        ".parquet",
                        ".csv",
                        ".sac",
                        ".tar",
                    ],
                    "delegates_to": ["sac_format"],
                },
            ),
        )

        self.registry.register_agent(
            "sac_format",
            self.sac_format_expert,
            AgentCapability(
                keywords=["sac", "waveform", "trace", "seismology", "seismic"],
                description=(
                    "Nested format expert for SAC waveform archives. Inspects SAC members, "
                    "computes trace statistics, and can provide plot-ready trace outputs."
                ),
                tools=[
                    "sac_inspect_archive",
                    "sac_fetch_earthscope_waveform",
                    "sac_compute_trace_statistics",
                    "sac_plot_traces",
                ],
                specialization="data_format",
                parent_id="analysis",
                source="builtin_nested",
                metadata={
                    "file_suffixes": [".sac", ".tar", ".tgz", ".gz"],
                    "format": "sac",
                    "future_model_boundary": True,
                },
            ),
        )

        self.registry.register_agent(
            "visualization",
            self.visualization_expert,
            AgentCapability(
                keywords=[
                    "plot",
                    "chart",
                    "histogram",
                    "scatter",
                    "visualization",
                    "graph",
                ],
                description=(
                    "Scientific data visualization expert for producing artifacts from "
                    "already available local data or analysis summaries. Does not own "
                    "dataset discovery, staging, or quantitative analysis prerequisites."
                ),
                tools=[
                    "plot_histogram",
                    "plot_bar_chart",
                    "plot_scatter",
                    "plot_summary",
                ],
                specialization="data_visualization",
                metadata={"file_suffixes": [".parquet", ".csv", ".sac", ".tar"]},
            ),
        )

        self.registry.register_agent(
            "genomics",
            self,
            AgentCapability(
                keywords=[
                    "genomics",
                    "genome",
                    "sequence",
                    "fasta",
                    "variant",
                    "vcf",
                    "mutation",
                    "sample",
                ],
                description=(
                    "Genomics review expert for small FASTA references and VCF variant files. "
                    "Uses sequence composition and variant-summary tools before synthesis."
                ),
                tools=[
                    "genomics_inspect_fasta",
                    "genomics_summarize_vcf",
                ],
                specialization="genomics",
                metadata={
                    "file_suffixes": [".fa", ".fasta", ".fna", ".vcf"],
                    "coordinated_file_suffixes": [".fa", ".fasta", ".fna", ".vcf"],
                },
            ),
        )

        self.registry.register_agent(
            "materials",
            self,
            AgentCapability(
                keywords=[
                    "materials",
                    "material",
                    "crystal",
                    "crystallography",
                    "structure",
                    "cif",
                    "unit cell",
                    "space group",
                    "atom site",
                    "density",
                ],
                description=(
                    "Materials/crystallography expert for CIF structure files. Uses "
                    "unit-cell, formula, species, and atom-site inspection before synthesis."
                ),
                tools=["materials_inspect_cif"],
                specialization="materials",
                metadata={
                    "file_suffixes": [".cif"],
                    "coordinated_file_suffixes": [".cif"],
                },
            ),
        )

        self.registry.register_agent(
            "geospatial",
            self,
            AgentCapability(
                keywords=[
                    "geospatial",
                    "geojson",
                    "spatial",
                    "geometry",
                    "map",
                    "coordinates",
                    "bbox",
                    "bounding box",
                    "feature",
                    "polygon",
                ],
                description=(
                    "Geospatial expert for GeoJSON feature collections. Uses feature, "
                    "geometry, property, and coordinate-bounds inspection before synthesis."
                ),
                tools=["geospatial_inspect_geojson"],
                specialization="geospatial",
                metadata={
                    "file_suffixes": [".geojson"],
                    "coordinated_file_suffixes": [".geojson"],
                },
            ),
        )

        self.registry.register_agent(
            "imaging",
            self,
            AgentCapability(
                keywords=[
                    "imaging",
                    "image",
                    "microscopy",
                    "micrograph",
                    "png",
                    "intensity",
                    "foreground",
                    "segmentation",
                    "region",
                    "pixel",
                ],
                description=(
                    "Scientific image expert for PNG microscopy-style fixtures. Uses image "
                    "dimensions, intensity, foreground, and region inspection before synthesis."
                ),
                tools=["imaging_inspect_png"],
                specialization="imaging",
                metadata={
                    "file_suffixes": [".png"],
                    "coordinated_file_suffixes": [".png"],
                },
            ),
        )

        self.registry.register_agent(
            "mass_spec",
            self,
            AgentCapability(
                keywords=[
                    "mass spectrometry",
                    "mass-spec",
                    "mzml",
                    "proteomics",
                    "spectra",
                    "spectrum",
                    "ms level",
                    "peptide",
                    "m/z",
                    "ion current",
                ],
                description=(
                    "Mass spectrometry expert for mzML spectra files. Uses spectrum counts, "
                    "MS levels, m/z ranges, peak counts, and ion-current inspection before synthesis."
                ),
                tools=["mass_spec_inspect_mzml"],
                specialization="mass_spectrometry",
                metadata={
                    "file_suffixes": [".mzml"],
                    "coordinated_file_suffixes": [".mzml"],
                },
            ),
        )

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
        session_context = self._get_session_context(question, session_id, tool_scope="chat")
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
            if selected not in SPECIAL_ROUTE_TARGETS and error_info is None:
                error_info = self._tool_error_info_from_trace(selected, trace)
            if error_info and not error_info.get("details", {}).get("partial", False):
                success = False
                error_msg = str(error_info.get("message") or "Tool execution failed.")
                answer = ""
        except Exception as e:
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
        self._active_trace = None

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
            answer = self._run_chat_agent(question, session_context, trace=trace)
            route = self._route_for_selected(
                "chat",
                "Session routing_mode='chat' forced the conversational path.",
                confidence=1.0,
            )
            return "chat", answer, None, None, route

        capabilities = self._build_capabilities_context(routing_mode=routing_mode)
        observations: list[dict[str, Any]] = []
        selected = "chat"
        last_expert_result: Any = None
        last_error_info: dict[str, Any] | None = None
        error_info: dict[str, Any] | None = None
        accumulated_nanoagents: list[dict[str, Any]] = []
        route = trace.route

        for step in range(self._agent_max_steps()):
            self._raise_if_cancelled("planner_before")
            try:
                action = self._plan_next_action(
                    question=question,
                    session_context=session_context,
                    file_context=file_context,
                    capabilities=capabilities,
                    observations=observations,
                )
            except RoutingError as planner_error:
                if not self._has_successful_execution_observation(observations):
                    raise
                answer = self._synthesize_agent_answer(
                    question=question,
                    session_context=session_context,
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
                return (
                    selected,
                    answer,
                    self._expert_result_with_nanoagents(
                        last_expert_result,
                        accumulated_nanoagents,
                    ),
                    error_info,
                    route,
                )
            self._raise_if_cancelled("planner_after")
            kind = self._coerce_text(action.get("action")).strip().lower()
            reason = self._coerce_text(action.get("reason")).strip()

            if kind == "tool":
                tool_name = self._coerce_text(action.get("tool")).strip()
                owning_expert = self._selected_expert_for_tool(tool_name)
                selected = self._parent_route_for_child(owning_expert) or owning_expert
                path_error = self._recovery_tool_path_scope_error(
                    tool_name,
                    action.get("args"),
                    observations,
                )
                if path_error is not None:
                    observations.append(
                        {
                            "step": step + 1,
                            "type": "planner_error",
                            "ok": False,
                            "result": path_error,
                        }
                    )
                    continue
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
                if self._should_promote_tool_action_to_expert(
                    tool_name,
                    selected=selected,
                    owning_expert=owning_expert,
                    observations=observations,
                    question=question,
                    file_context=file_context,
                ):
                    self._raise_if_cancelled("expert_before")
                    selected, answer, expert_result, error_info = self._dispatch_expert_action(
                        expert_id=selected,
                        question=question,
                        file_context=file_context,
                        session_context=self._session_context_with_observations(
                            session_context,
                            observations,
                        ),
                        trace=trace,
                    )
                    selected = self._parent_route_for_child(selected) or selected
                    self._extend_nanoagents(accumulated_nanoagents, expert_result)
                    last_expert_result = expert_result
                    last_error_info = error_info
                    self._raise_if_cancelled("expert_after")
                    route = self._route_for_selected(
                        selected,
                        reason
                        or (
                            f"Planner selected {tool_name}, promoted to the owning "
                            f"{selected} expert for scoped tool execution."
                        ),
                        confidence=0.75,
                    )
                    observations.append(
                        self._expert_loop_observation(
                            step=step + 1,
                            expert=selected,
                            answer=answer,
                            expert_result=expert_result,
                            error_info=error_info,
                            reason=reason,
                        )
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

            if kind == "expert":
                expert_id = self._coerce_text(action.get("expert")).strip().lower()
                expert_question = self._coerce_text(action.get("question")).strip() or question
                expert_question = self._repair_question_filepaths_from_context(
                    expert_question,
                    source_question=question,
                    file_context=file_context,
                )
                child_parent = self._parent_route_for_child(expert_id)
                if child_parent:
                    reason = (
                        reason
                        or f"Planner selected child expert {expert_id}; routing through parent."
                    )
                    expert_id = child_parent
                path_error = self._recovery_expert_path_scope_error(
                    expert_id,
                    expert_question,
                    observations,
                )
                if path_error is not None:
                    observations.append(
                        {
                            "step": step + 1,
                            "type": "planner_error",
                            "ok": False,
                            "result": path_error,
                        }
                    )
                    continue
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
                    answer = self._run_chat_agent(question, session_context, trace=trace)
                    route = self._route_for_selected(
                        "chat",
                        "Planner expert action ignored because no concrete file/data context exists.",
                        confidence=0.65,
                    )
                    return "chat", answer, None, None, route
                compatibility_error = self._expert_file_compatibility_error(
                    expert_id,
                    file_context,
                    question=expert_question,
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
                    session_context=self._session_context_with_observations(
                        session_context,
                        observations,
                    ),
                    trace=trace,
                )
                selected = self._parent_route_for_child(selected) or selected
                self._extend_nanoagents(accumulated_nanoagents, expert_result)
                last_expert_result = expert_result
                last_error_info = error_info
                self._raise_if_cancelled("expert_after")
                route = self._route_for_selected(
                    selected,
                    reason or f"Agent planner delegated to the {selected} expert.",
                    confidence=0.75,
                )
                observations.append(
                    self._expert_loop_observation(
                        step=step + 1,
                        expert=selected,
                        answer=answer,
                        expert_result=expert_result,
                        error_info=error_info,
                        reason=reason,
                    )
                )
                continue

            if kind == "none":
                if self._answer_would_ignore_recoverable_parent_action(question, observations):
                    observations.append(
                        {
                            "step": step + 1,
                            "type": "planner_error",
                            "ok": False,
                            "result": self._recoverable_parent_action_error(),
                        }
                    )
                    continue
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
                if self._answer_would_ignore_recoverable_parent_action(question, observations):
                    observations.append(
                        {
                            "step": step + 1,
                            "type": "planner_error",
                            "ok": False,
                            "result": self._recoverable_parent_action_error(),
                        }
                    )
                    continue
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
                    answer = self._fallback_answer_from_observations(observations)
                if not answer and last_error_info is not None:
                    route = self._route_for_selected(
                        selected,
                        reason or "Agent planner observed an expert failure.",
                        confidence=0.55,
                    )
                    return (
                        selected,
                        "",
                        self._expert_result_with_nanoagents(
                            last_expert_result,
                            accumulated_nanoagents,
                        ),
                        last_error_info,
                        route,
                    )
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
                return (
                    selected,
                    answer,
                    self._expert_result_with_nanoagents(
                        last_expert_result,
                        accumulated_nanoagents,
                    ),
                    last_error_info,
                    route,
                )

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
        answer = self._fallback_answer_from_observations(observations)
        if not answer:
            answer = self._synthesize_agent_answer(
                question=question,
                session_context=session_context,
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
        return (
            selected,
            answer,
            self._expert_result_with_nanoagents(last_expert_result, accumulated_nanoagents),
            error_info or last_error_info,
            route,
        )

    @classmethod
    def _answer_would_ignore_recoverable_parent_action(
        cls,
        question: str,
        observations: list[dict[str, Any]],
    ) -> bool:
        """Return whether a direct answer would stop before required recovery."""

        q_lower = question.lower()
        if not any(term in q_lower for term in ("plot", "artifact", "visual")):
            return False
        if not any(term in q_lower for term in ("seismic", "waveform", "sac", "miniseed")):
            return False
        if cls._observations_have_waveform_artifact(observations):
            return False
        return cls._observations_have_recoverable_ndp_blocker(observations)

    @classmethod
    def _observations_have_waveform_artifact(cls, observations: list[dict[str, Any]]) -> bool:
        """Return whether observations include a plotted waveform artifact."""

        for observation in observations:
            for path in observation.get("local_paths", []) or []:
                if cls._coerce_text(path).lower().endswith(".png"):
                    return True
            direct_tool = cls._coerce_text(observation.get("tool")).strip()
            direct_result = observation.get("result")
            if direct_tool == "sac_plot_traces" and tool_result_ok(direct_result):
                return True
            if any(
                cls._coerce_text(path).lower().endswith(".png")
                for path in cls._local_paths_from_value(direct_result)
            ):
                return True
            for tool in observation.get("tools", []) or []:
                if not isinstance(tool, Mapping):
                    continue
                tool_name = cls._coerce_text(tool.get("tool") or tool.get("name")).strip()
                result = tool.get("result")
                if tool_name == "sac_plot_traces" and tool_result_ok(result):
                    return True
                if any(
                    cls._coerce_text(path).lower().endswith(".png")
                    for path in cls._local_paths_from_value(result)
                ):
                    return True
        return False

    @classmethod
    def _observations_have_recoverable_ndp_blocker(
        cls,
        observations: list[dict[str, Any]],
    ) -> bool:
        """Return whether observations expose NDP staging failure with parent recovery."""

        recovery_actions = {
            "broaden_catalog_search",
            "try_another_provider",
            "delegate_to_utility_download",
        }
        for observation in observations:
            metadata = observation.get("metadata")
            staging = metadata.get("staging") if isinstance(metadata, Mapping) else None
            actions = observation.get("recommended_parent_actions", [])
            if isinstance(staging, Mapping):
                if staging.get("status") != "blocked":
                    continue
                actions = staging.get("recommended_parent_actions", actions)
            normalized_actions = {
                cls._coerce_text(action).strip()
                for action in actions or []
                if cls._coerce_text(action).strip()
            }
            if normalized_actions & recovery_actions:
                return True
        return False

    @staticmethod
    def _recoverable_parent_action_error() -> dict[str, Any]:
        """Return planner feedback for a recoverable parent-action blocker."""

        return {
            "message": (
                "A child expert returned recoverable parent actions, but the requested "
                "workflow still lacks a verified waveform plot artifact."
            ),
            "next_action": (
                "Do not answer yet. Choose a recovery action such as another provider "
                "or SAC waveform staging, then compute statistics or plot the resulting "
                "local waveform path."
            ),
            "available_recovery_tools": [
                "sac_fetch_earthscope_waveform",
                "sac_compute_trace_statistics",
                "sac_plot_traces",
            ],
        }

    @classmethod
    def _recovery_tool_path_scope_error(
        cls,
        tool_name: str,
        raw_args: Any,
        observations: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Reject SAC analysis/plotting of paths not produced by this recovery loop."""

        if tool_name not in {"sac_compute_trace_statistics", "sac_plot_traces"}:
            return None
        if not cls._observations_have_recoverable_ndp_blocker(observations):
            return None
        args = cls._normalize_tool_args(raw_args)
        filepath = cls._coerce_text(args.get("filepath")).strip()
        if not filepath:
            return None
        observed_paths = cls._observed_local_paths_from_observations(observations)
        if filepath in observed_paths:
            return None
        return {
            "message": (
                "Planner selected a SAC recovery tool with a filepath that was not "
                "produced by this turn's successful staging or recovery observations."
            ),
            "tool": tool_name,
            "filepath": filepath,
            "next_action": (
                "First stage or fetch a waveform with a recovery tool such as "
                "sac_fetch_earthscope_waveform, then use the returned local path."
            ),
            "observed_local_paths": sorted(observed_paths),
        }

    @classmethod
    def _recovery_expert_path_scope_error(
        cls,
        expert_id: str,
        expert_question: str,
        observations: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Reject expert recovery using unobserved local waveform paths."""

        if expert_id not in {"analysis", "visualization"}:
            return None
        if not cls._observations_have_recoverable_ndp_blocker(observations):
            return None
        observed_paths = cls._observed_local_paths_from_observations(observations)
        paths = [str(path) for path in extract_file_paths(expert_question, "", SAC_SUFFIXES)]
        if not paths:
            return {
                "message": (
                    "Planner selected a downstream recovery expert after an NDP "
                    "staging blocker without naming an explicit current-turn "
                    "waveform path."
                ),
                "expert": expert_id,
                "next_action": (
                    "First stage or fetch a waveform with a recovery tool such as "
                    "sac_fetch_earthscope_waveform, then pass the exact returned "
                    "local path to analysis or visualization."
                ),
                "observed_local_paths": sorted(observed_paths),
            }
        missing = [path for path in paths if path not in observed_paths]
        if not missing:
            return None
        return {
            "message": (
                "Planner selected an expert recovery step with waveform file paths "
                "that were not produced by this turn's successful staging or recovery "
                "observations."
            ),
            "expert": expert_id,
            "unobserved_paths": missing,
            "next_action": (
                "First stage or fetch a waveform with a recovery tool such as "
                "sac_fetch_earthscope_waveform, then pass the returned local path "
                "to analysis or visualization."
            ),
            "observed_local_paths": sorted(observed_paths),
        }

    @classmethod
    def _observed_local_paths_from_observations(
        cls,
        observations: list[dict[str, Any]],
    ) -> set[str]:
        """Collect local paths produced by current-turn observations."""

        paths: set[str] = set()
        for observation in observations:
            if observation.get("type") == "planner_error" or observation.get("ok") is False:
                continue
            for path in observation.get("local_paths", []) or []:
                text = cls._coerce_text(path).strip()
                if text:
                    paths.add(text)
            for path in cls._local_paths_from_value(observation.get("result")):
                paths.add(path)
            for tool in observation.get("tools", []) or []:
                if isinstance(tool, Mapping):
                    for path in cls._local_paths_from_value(tool.get("result")):
                        paths.add(path)
        return paths

    @staticmethod
    def _has_successful_execution_observation(observations: list[dict[str, Any]]) -> bool:
        """Return whether the loop has a completed non-planner observation to answer from."""
        return any(
            observation.get("type") != "planner_error" and observation.get("ok") is True
            for observation in observations
        )

    def _expert_loop_observation(
        self,
        *,
        step: int,
        expert: str,
        answer: str,
        expert_result: Any,
        error_info: dict[str, Any] | None,
        reason: str,
    ) -> dict[str, Any]:
        """Represent a completed expert delegation as planner-loop state."""
        tools: list[dict[str, Any]] = []
        local_paths: list[str] = []
        for observation in list(getattr(expert_result, "tool_provenance", []) or []) + list(
            getattr(expert_result, "tools", []) or []
        ):
            tool_name = self._coerce_text(getattr(observation, "tool", "")).strip()
            raw_result = getattr(observation, "result", None)
            ok = bool(getattr(observation, "ok", tool_result_ok(raw_result)))
            if ok:
                for path in self._local_paths_from_value(raw_result):
                    if path not in local_paths:
                        local_paths.append(path)
            tools.append(
                {
                    "tool": tool_name,
                    "ok": ok,
                    "result": compact_tool_result(raw_result, tool=tool_name or None, ok=ok),
                }
            )

        metadata = getattr(expert_result, "metadata", None)
        if not isinstance(metadata, Mapping):
            metadata = {}
        parent_actions: list[str] = []
        staging = metadata.get("staging")
        if isinstance(staging, Mapping):
            parent_actions = [
                self._coerce_text(action).strip()
                for action in staging.get("recommended_parent_actions", [])
                if self._coerce_text(action).strip()
            ]
        return {
            "step": step,
            "type": "expert",
            "expert": expert,
            "ok": error_info is None,
            "reason": reason,
            "answer": self._coerce_text(answer).strip()[:1800],
            "metadata": dict(metadata),
            "recommended_parent_actions": parent_actions,
            "tools": tools[-8:],
            "local_paths": local_paths[-8:],
            "error_info": error_info,
        }

    @classmethod
    def _local_paths_from_value(cls, value: Any) -> list[str]:
        """Extract local file paths from nested tool payloads for planner state."""
        paths: list[str] = []
        if isinstance(value, str):
            for match in re.findall(r"/(?:[^\s,\"'`]|\\ )+", value):
                cleaned = match.rstrip(".,;:)")
                if Path(cleaned).suffix.lower() in SCIENTIFIC_FILE_SUFFIXES:
                    paths.append(cleaned)
            return paths
        if isinstance(value, Mapping):
            for item in value.values():
                paths.extend(cls._local_paths_from_value(item))
            return paths
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                paths.extend(cls._local_paths_from_value(item))
        return paths

    def _session_context_with_observations(
        self,
        session_context: str,
        observations: list[dict[str, Any]],
    ) -> str:
        """Append compact current-turn observations for downstream experts."""
        if not observations:
            return session_context
        observation_context = self._format_observations_for_prompt(observations[-4:])
        if not session_context.strip():
            return f"[Current turn observations]\n{observation_context}"
        return (
            f"{session_context.strip()}\n\n"
            f"[Current turn observations]\n{observation_context}"
        )

    def _should_promote_tool_action_to_expert(
        self,
        tool_name: str,
        *,
        selected: str,
        owning_expert: str,
        observations: list[dict[str, Any]],
        question: str,
        file_context: str,
    ) -> bool:
        """Return whether a planner tool action should become expert delegation.

        Child expert tools should execute through the parent-owned delegation
        boundary on the first planner step. Letting the tier-1 planner iterate
        directly over provider/format tools makes the orchestrator behave like
        a flat tool-using expert and loses handoff boundaries.
        """

        caps = self.registry.get_capabilities(owning_expert)
        if caps is not None and caps.parent_id and not observations:
            return True
        if selected != "data" or not tool_name.startswith("ndp_"):
            return False
        return not ClioAgent._has_successful_execution_observation(observations)

    def _unique_agent_for_metadata_suffix(
        self,
        *,
        metadata_key: str,
        suffixes: set[str],
    ) -> str:
        """Return one agent whose metadata suffix set covers the requested suffixes."""
        candidates: list[str] = []
        for agent_id in self.registry.list_agents():
            caps = self.registry.get_capabilities(agent_id)
            if caps is None:
                continue
            supported = {
                str(suffix).lower()
                for suffix in caps.metadata.get(metadata_key, [])
                if str(suffix).strip()
            }
            if suffixes.issubset(supported):
                candidates.append(agent_id)
        return candidates[0] if len(candidates) == 1 else ""

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
        planner_context = self._planner_session_context(session_context)
        try:
            result = self._call_with_transient_provider_retries(
                "action_planner",
                lambda: self._call_action_planner(
                    question=self._planner_question(question),
                    session_context=planner_context,
                    file_context=file_context,
                    capabilities=capabilities,
                    observations=observations_text,
                ),
            )
            return self._parse_action_json(getattr(result, "action_json", ""))
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
                            session_context=planner_context,
                            file_context=file_context,
                            capabilities=retry_capabilities,
                            observations=observations_text,
                        ),
                    )
                    return self._parse_action_json(getattr(result, "action_json", ""))
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
        session_context: str,
        file_context: str,
        capabilities: str,
        observations: str,
    ) -> Any:
        """Invoke the DSPy/LiteLLM action planner."""
        with dspy.context(lm=self._planner_lm, adapter=self._dspy_adapter):
            return self.action_planner(
                question=question,
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
            "analysis, markdown, or prose outside the JSON object. For expert "
            "actions, set question to an empty string unless narrowing is required.\n"
            f"{question}"
        )

    def _planner_retry_question(self, question: str) -> str:
        """Return a stricter planner prompt for compact retry attempts."""
        if not self._uses_no_think_planner_profile():
            return question
        return (
            "/no_think\n"
            "Return exactly one minified JSON action. Prefer expert delegation "
            "over listing tool calls when the request is a natural multi-file "
            'scientific triage. For expert actions use question:"".\n'
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

    @classmethod
    def _expert_dispatch_context(cls, *, file_context: str, session_context: str) -> str:
        """Return scoped context passed from the orchestrator into an expert."""
        cleaned_session = cls._strip_context_sections(
            session_context,
            {"Available Tools", "Available Data"},
        ).strip()
        if cleaned_session == "No prior context":
            cleaned_session = ""
        parts: list[str] = []
        if file_context.strip():
            parts.append(file_context.strip())
        if cleaned_session:
            parts.append("[Retained session context]\n" + cleaned_session)
        return "\n\n".join(parts)

    def _dispatch_expert_action(
        self,
        *,
        expert_id: str,
        question: str,
        file_context: str,
        trace: RunTrace,
        session_context: str = "",
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
        expert_context = self._expert_dispatch_context(
            file_context=file_context,
            session_context=session_context,
        )
        dispatch_id = self._dispatch_target_for_expert(expert_id)
        try:
            with dspy.context(lm=self._main_lm, adapter=self._dspy_adapter):
                if dispatch_id == "data":
                    started = time.time()
                    expert_result = self._call_with_transient_provider_retries(
                        "expert_data",
                        lambda: self.data_expert(
                            question=expert_question,
                            file_context=expert_context,
                        ),
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    self._merge_expert_provenance(trace, expert_result)
                    answer = (
                        f"{expert_result.analysis}\n\n"
                        f"Recommendations:\n{expert_result.recommendations}"
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "analysis":
                    started = time.time()
                    expert_result = self._call_with_transient_provider_retries(
                        "expert_analysis",
                        lambda: self.analysis_expert(
                            question=expert_question,
                            file_context=expert_context,
                        ),
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    self._merge_expert_provenance(trace, expert_result)
                    answer = (
                        f"{expert_result.analysis}\n\n"
                        f"Recommendations:\n{expert_result.recommendations}"
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "ndp_catalog":
                    started = time.time()
                    expert_result = self._call_with_transient_provider_retries(
                        "expert_ndp_catalog",
                        lambda: self.ndp_catalog_expert(
                            question=expert_question,
                            file_context=expert_context,
                        ),
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    self._merge_expert_provenance(trace, expert_result)
                    answer = (
                        f"{expert_result.analysis}\n\n"
                        f"Recommendations:\n{expert_result.recommendations}"
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "sac_format":
                    started = time.time()
                    expert_result = self._call_with_transient_provider_retries(
                        "expert_sac_format",
                        lambda: self.sac_format_expert(
                            question=expert_question,
                            file_context=expert_context,
                        ),
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    self._merge_expert_provenance(trace, expert_result)
                    answer = (
                        f"{expert_result.analysis}\n\n"
                        f"Recommendations:\n{expert_result.recommendations}"
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "genomics":
                    started = time.time()
                    paths = extract_file_paths(
                        expert_question,
                        expert_context,
                        {".fa", ".fasta", ".fna", ".vcf"},
                    )
                    fasta_paths = [
                        path
                        for path in paths
                        if Path(str(path)).suffix.lower() in {".fa", ".fasta", ".fna"}
                    ]
                    vcf_paths = [
                        path for path in paths if Path(str(path)).suffix.lower() == ".vcf"
                    ]
                    observations: list[str] = []
                    if fasta_paths:
                        fasta_result = self._execute_tool_action(
                            "genomics_inspect_fasta",
                            {"filepath": str(fasta_paths[0])},
                            trace,
                            question=expert_question,
                            file_context=expert_context,
                        )
                        observations.append(
                            "FASTA: " + json.dumps(compact_tool_result(fasta_result, max_text=900))
                        )
                    if vcf_paths:
                        vcf_result = self._execute_tool_action(
                            "genomics_summarize_vcf",
                            {"filepath": str(vcf_paths[0])},
                            trace,
                            question=expert_question,
                            file_context=expert_context,
                        )
                        observations.append(
                            "VCF: " + json.dumps(compact_tool_result(vcf_result, max_text=900))
                        )
                    if not observations:
                        return (
                            expert_id,
                            "",
                            None,
                            RoutingError(
                                "The genomics expert needs a FASTA, FNA, FA, or VCF file path.",
                                details=self._recovery_details(
                                    expert=expert_id,
                                    next_action=(
                                        "Provide at least one FASTA/FNA/FA or VCF file under "
                                        "the allowed workspace roots."
                                    ),
                                ),
                            ).to_dict(),
                        )
                    answer = (
                        "Genomics review:\n"
                        + "\n\n".join(observations)
                        + "\n\nRecommendations:\n"
                        "- Review PASS variants with high-impact EFFECT labels first.\n"
                        "- Compare variant positions against the named contigs before downstream use."
                    )
                    expert_result = dspy.Prediction(
                        analysis=answer,
                        recommendations="Validate high-impact calls and confirm sample provenance.",
                        metadata={"expert": "genomics", "paths": [str(path) for path in paths]},
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "materials":
                    started = time.time()
                    paths = extract_file_paths(expert_question, expert_context, {".cif"})
                    material_observations: list[str] = []
                    if paths:
                        cif_result = self._execute_tool_action(
                            "materials_inspect_cif",
                            {"filepath": str(paths[0])},
                            trace,
                            question=expert_question,
                            file_context=expert_context,
                        )
                        material_observations.append(
                            "CIF: " + json.dumps(compact_tool_result(cif_result, max_text=1000))
                        )
                    if not material_observations:
                        return (
                            expert_id,
                            "",
                            None,
                            RoutingError(
                                "The materials expert needs a CIF file path.",
                                details=self._recovery_details(
                                    expert=expert_id,
                                    next_action=(
                                        "Provide at least one CIF file under the allowed "
                                        "workspace roots."
                                    ),
                                ),
                            ).to_dict(),
                        )
                    answer = (
                        "Materials structure review:\n"
                        + "\n\n".join(material_observations)
                        + "\n\nRecommendations:\n"
                        "- Verify the space group and unit-cell parameters against provenance.\n"
                        "- Confirm occupancies/species before simulation or collaborator handoff."
                    )
                    expert_result = dspy.Prediction(
                        analysis=answer,
                        recommendations="Validate symmetry, occupancies, and structure provenance.",
                        metadata={"expert": "materials", "paths": [str(path) for path in paths]},
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "geospatial":
                    started = time.time()
                    paths = extract_file_paths(expert_question, expert_context, {".geojson"})
                    geospatial_observations: list[str] = []
                    if paths:
                        geojson_result = self._execute_tool_action(
                            "geospatial_inspect_geojson",
                            {"filepath": str(paths[0])},
                            trace,
                            question=expert_question,
                            file_context=expert_context,
                        )
                        geospatial_observations.append(
                            "GeoJSON: "
                            + json.dumps(compact_tool_result(geojson_result, max_text=1000))
                        )
                    if not geospatial_observations:
                        return (
                            expert_id,
                            "",
                            None,
                            RoutingError(
                                "The geospatial expert needs a GeoJSON file path.",
                                details=self._recovery_details(
                                    expert=expert_id,
                                    next_action=(
                                        "Provide at least one GeoJSON file under the allowed "
                                        "workspace roots."
                                    ),
                                ),
                            ).to_dict(),
                        )
                    answer = (
                        "Geospatial data review:\n"
                        + "\n\n".join(geospatial_observations)
                        + "\n\nRecommendations:\n"
                        "- Verify coordinate reference system assumptions before map overlay.\n"
                        "- Check property completeness for features used in downstream analysis."
                    )
                    expert_result = dspy.Prediction(
                        analysis=answer,
                        recommendations="Validate CRS assumptions, bounds, and feature properties.",
                        metadata={"expert": "geospatial", "paths": [str(path) for path in paths]},
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "imaging":
                    started = time.time()
                    paths = extract_file_paths(expert_question, expert_context, {".png"})
                    image_observations: list[str] = []
                    if paths:
                        image_result = self._execute_tool_action(
                            "imaging_inspect_png",
                            {"filepath": str(paths[0])},
                            trace,
                            question=expert_question,
                            file_context=expert_context,
                        )
                        image_observations.append(
                            "PNG: " + json.dumps(compact_tool_result(image_result, max_text=1000))
                        )
                    if not image_observations:
                        return (
                            expert_id,
                            "",
                            None,
                            RoutingError(
                                "The imaging expert needs a PNG file path.",
                                details=self._recovery_details(
                                    expert=expert_id,
                                    next_action=(
                                        "Provide at least one PNG file under the allowed "
                                        "workspace roots."
                                    ),
                                ),
                            ).to_dict(),
                        )
                    answer = (
                        "Scientific image review:\n"
                        + "\n\n".join(image_observations)
                        + "\n\nRecommendations:\n"
                        "- Verify acquisition scale and channel meaning before quantitative use.\n"
                        "- Check foreground segmentation assumptions before downstream analysis."
                    )
                    expert_result = dspy.Prediction(
                        analysis=answer,
                        recommendations=(
                            "Validate acquisition metadata, foreground threshold, and region evidence."
                        ),
                        metadata={"expert": "imaging", "paths": [str(path) for path in paths]},
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "mass_spec":
                    started = time.time()
                    paths = extract_file_paths(expert_question, expert_context, {".mzml"})
                    mzml_observations: list[str] = []
                    if paths:
                        mzml_result = self._execute_tool_action(
                            "mass_spec_inspect_mzml",
                            {"filepath": str(paths[0])},
                            trace,
                            question=expert_question,
                            file_context=expert_context,
                        )
                        mzml_observations.append(
                            "mzML: " + json.dumps(compact_tool_result(mzml_result, max_text=1000))
                        )
                        qc_sentence = _mass_spec_qc_sentence(mzml_result)
                        if qc_sentence:
                            mzml_observations.append(qc_sentence)
                    if not mzml_observations:
                        return (
                            expert_id,
                            "",
                            None,
                            RoutingError(
                                "The mass spectrometry expert needs an mzML file path.",
                                details=self._recovery_details(
                                    expert=expert_id,
                                    next_action=(
                                        "Provide at least one mzML file under the allowed "
                                        "workspace roots."
                                    ),
                                ),
                            ).to_dict(),
                        )
                    answer = (
                        "Mass spectrometry data review:\n"
                        + "\n\n".join(mzml_observations)
                        + "\n\nRecommendations:\n"
                        "- Verify instrument/acquisition metadata before peptide search.\n"
                        "- Check MS-level balance, peak counts, and TIC consistency before handoff."
                    )
                    expert_result = dspy.Prediction(
                        analysis=answer,
                        recommendations=(
                            "Validate acquisition metadata, MS levels, m/z coverage, and TIC evidence."
                        ),
                        metadata={"expert": "mass_spec", "paths": [str(path) for path in paths]},
                    )
                    self._record_expert_handoff(
                        trace,
                        expert_id=expert_id,
                        dispatch_target=dispatch_id,
                        stage="planner_dispatch",
                        input_summary=expert_question,
                        result=expert_result,
                        duration_ms=(time.time() - started) * 1000,
                    )
                    return expert_id, answer, expert_result, None

                if dispatch_id == "utility":
                    return (
                        expert_id,
                        "",
                        None,
                        RoutingError(
                            "Utility requests must call a concrete tool such as shell_bash.",
                            details=self._recovery_details(
                                expert=expert_id,
                                next_action="Select action='tool' with tool='shell_bash'.",
                            ),
                        ).to_dict(),
                    )

                started = time.time()
                expert_result = self._call_with_transient_provider_retries(
                    "expert_visualization",
                    lambda: self.visualization_expert(
                        question=expert_question,
                        file_context=expert_context,
                    ),
                )
                self._record_expert_handoff(
                    trace,
                    expert_id=expert_id,
                    dispatch_target=dispatch_id,
                    stage="planner_dispatch",
                    input_summary=expert_question,
                    result=expert_result,
                    duration_ms=(time.time() - started) * 1000,
                )
                self._merge_expert_provenance(trace, expert_result)
            description = self._coerce_text(
                getattr(expert_result, "visualization_description", "")
            ).strip()
            file_path = self._coerce_text(getattr(expert_result, "file_path", "")).strip()
            answer = f"Visualization: {description}\n\nFile: {file_path}".strip()
            error_info = getattr(expert_result, "error_info", None)
            if not isinstance(error_info, dict):
                error_info = None
            return (
                expert_id,
                answer,
                expert_result,
                error_info,
            )
        except CancellationError:
            raise
        except Exception as exc:
            self._record_expert_handoff(
                trace,
                expert_id=expert_id,
                dispatch_target=dispatch_id,
                stage="planner_dispatch",
                input_summary=expert_question,
                status="failure",
                error=str(exc),
            )
            error = ExpertError(
                f"The {expert_id} expert encountered an issue processing your request.",
                details=self._recovery_details(
                    expert=expert_id,
                    dispatch_target=dispatch_id,
                    original_error=str(exc),
                ),
            ).to_dict()
            return expert_id, "", None, error

    def _dispatch_target_for_expert(self, expert_id: str) -> str:
        """Return the executable expert target for a registered expert id."""
        caps = self.registry.get_capabilities(expert_id)
        if caps is None:
            return expert_id
        target = self._coerce_text(caps.metadata.get("dispatch_to")).strip().lower()
        return target or expert_id

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

        owner = self._selected_expert_for_tool(tool_name)
        if tool_name in visualization_tools:
            result = self._execute_visualization_tool(
                tool_name,
                visualization_tools[tool_name],
                args,
            )
            self._record_direct_tool_handoff(
                trace,
                expert_id=owner,
                tool_name=tool_name,
                args=args,
                result=result,
                duration_ms=self._last_tool_duration_ms(trace, tool_name),
            )
            return result

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
        self._record_direct_tool_handoff(
            trace,
            expert_id=owner,
            tool_name=tool_name,
            args=args,
            result=result,
            duration_ms=duration_ms,
        )
        return result

    @staticmethod
    def _last_tool_duration_ms(trace: RunTrace, tool_name: str) -> float:
        """Return the latest recorded duration for a tool in an active trace."""
        for observation in reversed(trace.tools):
            if observation.tool == tool_name:
                return observation.duration_ms
        return 0.0

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
                "Use the native scientific tools for the selected expert, or route to "
                "the appropriate data/analysis/visualization expert."
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

    def _execute_visualization_tool(self, tool_name: str, tool: Any, args: dict[str, Any]) -> Any:
        """Execute one local visualization tool with policy-aware artifact defaults."""
        args = dict(args)
        self._raise_if_cancelled("visualization_tool_before")
        filepath = self._coerce_text(args.get("filepath")).strip()
        if filepath and not self._coerce_text(args.get("output_path")).strip():
            prepared = self._prepare_visualization_output_path(tool_name, filepath)
            if isinstance(prepared, dict) and "error" in prepared:
                start = time.time()
                notify_global_tool_observer(tool_name, args, "started", None)
                notify_global_tool_observer(tool_name, args, "completed", repr(prepared["error"]))
                self._record_tool_call(tool_name, args, prepared, (time.time() - start) * 1000)
                return prepared
            args["output_path"] = str(prepared)

        start = time.time()
        notify_global_tool_observer(tool_name, args, "started", None)
        try:
            result = normalize_tool_result(
                self._call_tool_function(tool, **args),
                tool=tool_name,
            )
            self._raise_if_cancelled("visualization_tool_after")
        except CancellationError as exc:
            notify_global_tool_observer(tool_name, args, "completed", repr(exc))
            raise
        except Exception as exc:
            result = {"error": normalize_tool_error(exc, tool=tool_name, code="tool_exception")}
            notify_global_tool_observer(tool_name, args, "completed", repr(exc))
        else:
            notify_global_tool_observer(tool_name, args, "completed", None)
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
        answer_question = self._answer_synthesis_question(question)
        try:
            with dspy.context(lm=self._main_lm, adapter=self._dspy_adapter):
                result = self._call_with_transient_provider_retries(
                    "answer_synthesizer",
                    lambda: self.answer_synthesizer(
                        question=answer_question,
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
            if observation.get("type") == "expert":
                answer = cls._coerce_text(observation.get("answer")).strip()
                expert = cls._coerce_text(observation.get("expert")).strip()
                if answer:
                    lines.append(f"{expert} expert returned:\n{answer}" if expert else answer)
                continue
            tool = cls._coerce_text(observation.get("tool")).strip()
            result = observation.get("result")
            if not tool or result in (None, ""):
                continue
            if isinstance(result, dict) and "error" in result:
                continue

            label = cls._tool_label_for_fallback(tool)
            scalar_summary = cls._scalar_observation_summary(result)
            if scalar_summary:
                lines.append(f"{label} ({tool}) returned: {scalar_summary}.")
            else:
                lines.append(f"{label} ({tool}) returned a successful result.")

            ndp_names = cls._ndp_dataset_names(result)
            if ndp_names:
                lines.append(f"National Data Platform datasets: {', '.join(ndp_names[:5])}.")

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
        raw = os.environ.get("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "5,15").strip()
        if raw.lower() in {"", "false", "off", "none", "disabled"}:
            return ()
        delays: list[float] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                delay = float(item)
            except ValueError:
                continue
            delays.append(max(0.0, min(delay, 60.0)))
        return tuple(delays)

    @staticmethod
    def _tool_label_for_fallback(tool_name: str) -> str:
        """Return a user-facing family label for an observed tool."""
        if tool_name.startswith("ndp_"):
            return "National Data Platform"
        if tool_name.startswith("hdf5_"):
            return "HDF5"
        if tool_name.startswith("adios_"):
            return "ADIOS/BP"
        if tool_name.startswith("parquet_"):
            return "Parquet"
        if tool_name.startswith("csv_"):
            return "CSV"
        if tool_name.startswith("plot_"):
            return "Visualization"
        if tool_name.startswith("shell_"):
            return "Utility"
        if tool_name.startswith("fs_"):
            return "Workspace"
        return "Tool"

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

    @classmethod
    def _ndp_dataset_names(cls, value: Any) -> list[str]:
        """Extract dataset names from common NDP result shapes."""
        if not isinstance(value, dict):
            return []
        datasets = value.get("datasets")
        if isinstance(datasets, dict):
            items = datasets.get("items", [])
        elif isinstance(datasets, list):
            items = datasets
        else:
            items = []
        names: list[str] = []
        for item in items:
            if isinstance(item, dict):
                name = cls._coerce_text(item.get("name") or item.get("title")).strip()
                if name:
                    names.append(name)
        return names

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
        mode = str(_ROUTING_MODE_OVERRIDE.get() or "").strip().lower()
        if not mode:
            mode = str(getattr(self, "_routing_mode_override", "auto") or "auto").strip().lower()
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
            "- Child experts are not root route targets. When a child capability is needed, "
            "delegate to its parent expert and let the parent synchronously call the child "
            "with the same task context."
        )
        lines.append(
            "Routing strategy: choose the expert that owns the next unresolved phase, "
            "not the expert that owns the final deliverable. For multi-phase work, "
            "delegate one phase, observe its result, then plan the next phase from "
            "the updated state. Do not skip data acquisition/discovery before "
            "analysis, and do not skip analysis before visualization."
        )
        lines.append(
            "Observation rule: local_paths in observations are newly available files. "
            "Use them for the next phase instead of repeating the same discovery or "
            "staging expert, while preserving source/provenance caveats."
        )
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
            str(intent)
            for intent in metadata.get("coordinator_intents", [])
            if str(intent).strip()
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
        """Return gateway and local visualization tools visible to the planner."""
        return [
            tool
            for tool in [
                *self.tool_executor.to_dspy_tools(),
                *self._visualization_tool_map().values(),
            ]
            if tool.name not in PLANNER_HIDDEN_TOOL_NAMES
        ]

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
        owner = tool_owner(tool_name)
        if owner and owner in self.registry.list_agents():
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

    def _expert_file_compatibility_error(
        self,
        expert_id: str,
        file_context: str,
        *,
        question: str = "",
    ) -> dict[str, Any] | None:
        """Reject expert delegation that cannot inspect the current file context."""
        paths = extract_file_paths(
            "\n".join(part for part in (question, file_context) if part),
            "",
            SCIENTIFIC_FILE_SUFFIXES,
        )
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
        supported.update(self._child_file_suffixes(expert_id))
        if not supported:
            return None

        unsupported = [str(path) for path in paths if path.suffix.lower() not in supported]
        if not unsupported:
            return None
        coordinated = {
            str(suffix).lower()
            for suffix in caps.metadata.get("coordinated_file_suffixes", [])
            if str(suffix).strip()
        }
        if (
            coordinated
            and {path.suffix.lower() for path in paths}.issubset(coordinated)
            and len(paths) > 1
        ):
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

    def _child_file_suffixes(self, parent_id: str) -> set[str]:
        """Return file suffixes handled by child experts under a parent."""
        suffixes: set[str] = set()
        for agent_id in self.registry.list_agents():
            caps = self.registry.get_capabilities(agent_id)
            if caps is None or caps.parent_id != parent_id:
                continue
            suffixes.update(
                str(suffix).lower()
                for suffix in caps.metadata.get("file_suffixes", [])
                if str(suffix).strip()
            )
        return suffixes

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
    def _repair_question_filepaths_from_context(
        text: str,
        *,
        source_question: str,
        file_context: str,
    ) -> str:
        """Repair degraded file paths in a planner-rewritten expert question."""
        repaired = text
        source_paths = extract_file_paths(source_question, file_context, SCIENTIFIC_FILE_SUFFIXES)
        if not source_paths:
            return repaired

        for source_path in source_paths:
            replacement = str(source_path.expanduser())
            if replacement in repaired or not source_path.name:
                continue
            updated = ClioAgent._replace_degraded_path_token(
                repaired,
                source_path.name,
                replacement,
            )
            if updated != repaired:
                repaired = updated

        for degraded in extract_file_paths(text, "", SCIENTIFIC_FILE_SUFFIXES):
            expanded = degraded.expanduser()
            if (expanded.exists() and degraded.is_absolute()) or not degraded.name:
                continue
            matches = [
                candidate
                for candidate in source_paths
                if candidate.name == degraded.name and candidate.expanduser().exists()
            ]
            if len(matches) == 1:
                replacement = str(matches[0].expanduser())
                if str(degraded) in repaired:
                    repaired = repaired.replace(str(degraded), replacement)
                else:
                    repaired = ClioAgent._replace_degraded_path_token(
                        repaired,
                        degraded.name,
                        replacement,
                    )
        return repaired

    @staticmethod
    def _replace_degraded_path_token(text: str, basename: str, replacement: str) -> str:
        """Replace a malformed path token ending in basename with replacement."""
        pattern = re.compile(rf"(?:[A-Za-z]:)?[^\s'\"`]*{re.escape(basename)}")
        return pattern.sub(lambda _match: replacement, text, count=1)

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
        if action not in {"tool", "expert", "answer", "none"}:
            raise ValueError(f"Planner returned unsupported action: {decoded!r}")
        decoded["action"] = action
        return decoded

    @classmethod
    def _repair_truncated_action_json(cls, text: str) -> dict[str, Any] | None:
        """Repair a planner JSON object that ended before final delimiters.

        This intentionally accepts only a single object that starts at the
        first character. It may close an unterminated string and missing
        brackets/braces, then normal action validation still decides whether
        the repaired object is usable.
        """

        truncated_key = cls._truncated_string_key(text)
        repaired = cls._close_truncated_json(text)
        if repaired is None or repaired == text:
            return None
        try:
            decoded = json.loads(repaired)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, dict):
            return None
        if (
            truncated_key == "question"
            and cls._coerce_text(decoded.get("action")).strip().lower() != "expert"
        ):
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

        if in_string and ClioAgent._unterminated_string_key(text, string_start) not in {
            "question",
            "reason",
        }:
            return None
        suffix = '"' if in_string else ""
        suffix += "".join(reversed(stack))
        if not suffix:
            return None
        return text + suffix

    @staticmethod
    def _truncated_string_key(text: str) -> str | None:
        """Return the key for an unterminated trailing string, if present."""

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
        if not in_string:
            return None
        return ClioAgent._unterminated_string_key(text, string_start)

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
        output_summary = self._expert_result_summary(result)
        parent_id = self._registered_parent_id(expert_id)
        trace.record_expert_handoff(
            agent_id=expert_id,
            parent_id=parent_id,
            dispatch_target=dispatch_target,
            stage=stage,
            status=status,
            input_summary=self._compact_handoff_text(input_summary),
            output_summary=output_summary,
            duration_ms=duration_ms,
            error=error,
            metadata=metadata,
        )

        reported_expert = self._coerce_text(metadata.get("expert")).strip().lower()
        if not reported_expert or reported_expert == expert_id:
            return
        if reported_expert in {row.agent_id for row in trace.expert_handoffs}:
            return
        reported_parent = self._coerce_text(metadata.get("parent_expert")).strip().lower()
        child_parent_id = reported_parent or expert_id
        lifecycle_metadata = {
            **metadata,
            "observed_through": expert_id,
            "delegation_lifecycle": "sync",
            "delegate_parent_id": child_parent_id,
            "delegate_child_id": reported_expert,
        }
        trace.record_expert_handoff(
            agent_id=reported_expert,
            parent_id=child_parent_id,
            dispatch_target=reported_expert,
            stage=f"{stage}_child",
            status=status,
            input_summary=self._compact_handoff_text(input_summary),
            output_summary=output_summary,
            duration_ms=duration_ms,
            error=error,
            metadata=lifecycle_metadata,
        )
        trace.record_expert_handoff(
            agent_id=reported_expert,
            parent_id=child_parent_id,
            dispatch_target=reported_expert,
            stage="delegate.completed" if status == "success" else "delegate.failed",
            status=status,
            input_summary=self._compact_handoff_text(input_summary),
            output_summary=output_summary,
            duration_ms=duration_ms,
            error=error,
            metadata={
                **lifecycle_metadata,
                "return_to": child_parent_id,
                "return_payload": "compact_result",
            },
        )
        trace.record_expert_handoff(
            agent_id=expert_id,
            parent_id=self._registered_parent_id(expert_id),
            dispatch_target=expert_id,
            stage="parent.resumed",
            status=status,
            input_summary=self._compact_handoff_text(input_summary),
            output_summary=output_summary,
            duration_ms=0.0,
            error=error,
            metadata={
                "delegation_lifecycle": "sync",
                "resumed_from": reported_expert,
                "child_parent_id": child_parent_id,
                "return_payload": "compact_result",
            },
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

    @classmethod
    def _expert_result_summary(cls, result: Any | None) -> str:
        """Return a compact human-readable expert output summary."""
        if result is None:
            return ""
        candidates = (
            getattr(result, "analysis", ""),
            getattr(result, "visualization_description", ""),
            getattr(result, "answer", ""),
        )
        for candidate in candidates:
            text = cls._coerce_text(candidate).strip()
            if text:
                return cls._compact_handoff_text(text)
        file_path = cls._coerce_text(getattr(result, "file_path", "")).strip()
        if file_path:
            return cls._compact_handoff_text(f"Artifact: {file_path}")
        return ""

    @staticmethod
    def _compact_handoff_text(text: str, *, limit: int = 500) -> str:
        """Compact one handoff field for durable metadata."""
        normalized = " ".join(str(text).split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 15].rstrip() + "...[truncated]"

    def _run_chat_agent(
        self,
        question: str,
        session_context: str,
        *,
        trace: RunTrace | None = None,
    ) -> str:
        """Generate a conversational reply through DSPy/LiteLLM."""
        self._raise_if_cancelled("chat_before")
        chat_context = self._chat_session_context(session_context)
        try:
            with dspy.context(lm=self._main_lm, adapter=self._dspy_adapter):
                if self._chat_should_use_utility_tools(question, session_context):
                    tool_agent = self._build_chat_tool_agent(
                        trace=trace,
                        question=question,
                        session_context=session_context,
                    )
                    result = tool_agent(question=question, session_context=chat_context)
                else:
                    result = self.chat_agent(question=question, session_context=chat_context)
            answer = self._coerce_text(getattr(result, "answer", None)).strip()
            self._raise_if_cancelled("chat_after")
            if answer:
                if self._question_requests_summary(question):
                    summary = self._summarize_assistant_context(chat_context)
                    if summary:
                        return summary
                return answer
            raise ValueError("Chat agent returned an empty answer.")
        except Exception as chat_error:
            recovered = self._parse_answer_from_adapter_error(chat_error)
            if recovered:
                self._raise_if_cancelled("chat_after")
                if self._question_requests_summary(question):
                    summary = self._summarize_assistant_context(chat_context)
                    if summary:
                        return summary
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
        """Return structured error_info for unrecovered failed tools in a trace."""
        last_success_index = -1
        for index, observation in enumerate(trace.tools):
            if observation.ok:
                last_success_index = index
        successful_tools = [tool.tool for tool in trace.tools[: last_success_index + 1] if tool.ok]
        for observation in trace.tools[last_success_index + 1 :]:
            if observation.ok:
                continue
            if cls._tool_error_observation_handled(observation):
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
    def _tool_error_observation_handled(observation: ToolObservation) -> bool:
        """Return whether an expert explicitly recovered this failed observation."""
        normalized = normalize_tool_result(observation.result, tool=observation.tool)
        if not isinstance(normalized, dict) or "error" not in normalized:
            return False
        error = normalize_tool_error(normalized["error"], tool=observation.tool)
        return bool(error.get("handled"))

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
        params = self._bind_tool_params(tool, args, kwargs)
        self._raise_if_cancelled("local_tool_before")
        start = time.time()
        notify_global_tool_observer(name, params, "started", None)
        try:
            result = self._call_tool_function(tool, *args, **kwargs)
            self._raise_if_cancelled("local_tool_after")
        except Exception as exc:
            notify_global_tool_observer(name, params, "completed", repr(exc))
            raise
        notify_global_tool_observer(name, params, "completed", None)
        duration_ms = (time.time() - start) * 1000
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

    def _get_session_context(
        self,
        question: str,
        session_id: str,
        tier: int = 2,
        tool_scope: str = "none",
    ) -> str:
        """Retrieve compiled session context from ARC Memory.

        Uses ContextCompiler pipeline (filter -> compact -> enrich -> assemble)
        with token budgets per tier. Falls back to "No prior context" on error.

        Args:
            question: User's current question
            session_id: Session identifier
            tier: Agent tier for token budget (1=planner/2K, 2=expert/4K)
            tool_scope: Agent/tool visibility scope for ARC tool summaries.

        Returns:
            Compiled context string or "No prior context"
        """
        try:
            compiled = self.context_retriever.compile_expert_context(
                query=question,
                session_id=session_id,
                tier=tier,
                tool_scope=tool_scope,
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
        if len(self._requested_file_families(question)) > 1:
            return None
        suffixes = self._requested_file_suffixes(question)
        return self._last_session_file_path(session_id, suffixes=suffixes)

    @staticmethod
    def _requested_file_families(question: str) -> set[str]:
        """Infer the scientific file families a natural follow-up requests."""
        lowered = question.lower()
        families: set[str] = set()
        if "parquet" in lowered:
            families.add("parquet")
        if "hdf5" in lowered or "h5" in lowered:
            families.add("hdf5")
        if "adios" in lowered or "bp5" in lowered or " bp" in lowered:
            families.add("adios")
        if "csv" in lowered:
            families.add("csv")
        return families

    @staticmethod
    def _requested_file_suffixes(question: str) -> set[str]:
        """Infer requested file type from natural follow-up wording."""
        families = ClioAgent._requested_file_families(question)
        suffixes: set[str] = set()
        if "parquet" in families:
            suffixes.add(".parquet")
        if "hdf5" in families:
            suffixes.update({".h5", ".hdf5"})
        if "adios" in families:
            suffixes.update({".bp", ".bp4", ".bp5"})
        if "csv" in families:
            suffixes.add(".csv")
        return suffixes or SCIENTIFIC_FILE_SUFFIXES

    def _last_session_file_path(
        self,
        session_id: str,
        *,
        suffixes: set[str] | None = None,
    ) -> Path | None:
        """Find the last local scientific file path mentioned in this session."""
        suffix_filter = suffixes or SCIENTIFIC_FILE_SUFFIXES
        try:
            conv = self.arc.get_conversation(session_id)
        except Exception:
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

    @staticmethod
    def _question_with_file_context(question: str, file_context: str) -> str:
        """Append current file context when a planner expert action omits the path."""
        if extract_file_paths(question, "", SCIENTIFIC_FILE_SUFFIXES):
            return question
        if len(ClioAgent._requested_file_families(question)) > 1:
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
    def _extend_nanoagents(target: list[dict[str, Any]], prediction: Any) -> None:
        """Append nanoagent spawns from a prediction without duplicating wire rows."""
        for spawn in ClioAgent._extract_nanoagents_spawned(prediction):
            if spawn not in target:
                target.append(spawn)

    @staticmethod
    def _expert_result_with_nanoagents(
        prediction: Any,
        nanoagents_spawned: list[dict[str, Any]],
    ) -> Any:
        """Return a prediction carrying all nanoagent spawns observed this turn."""
        if not nanoagents_spawned:
            return prediction
        if prediction is None:
            return dspy.Prediction(nanoagents_spawned=nanoagents_spawned)
        try:
            prediction.nanoagents_spawned = nanoagents_spawned  # type: ignore[attr-defined]
            return prediction
        except Exception:
            return dspy.Prediction(nanoagents_spawned=nanoagents_spawned)

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
