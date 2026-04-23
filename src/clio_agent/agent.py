"""
ClioAgent - Main Agent Module

Router + ChatAgent + Expert dispatch architecture.

Architecture:
    User Query -> Router (fast SLM, Literal output)
        -> "data" -> DataExpert (ReAct + HDF5 MCP tools)
        -> "analysis" -> AnalysisExpert (ReAct + Parquet MCP tools)
        -> "visualization" -> VisualizationExpert (ReAct + matplotlib tools)
        -> "chat" -> ChatAgent (CoT conversational)
        -> "none" -> Out-of-scope fallback message

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

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import dspy

from clio_agent.arc.lsm import LSMTree
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.retrieval import ContextRetriever
from clio_agent.arc.schema import (
    Conversation,
    Invocation,
    Message,
    RoutingDecision,
)
from clio_agent.config import (
    create_router_lm,
    fetch_lm_studio_models,
    load_config_from_env,
    select_models_for_agents,
)
from clio_agent.errors import (
    ExpertError,
    RoutingError,
)
from clio_agent.experts import AnalysisExpert, DataExpert, VisualizationExpert
from clio_agent.optimizer.instrumentation import _extract_output
from clio_agent.registry.registry import AgentCapability, AgentRegistry
from clio_agent.signatures.main_agent_sig import ChatAgentSignature, RouterSignature

_FILE_PATH_RE = re.compile(
    r"(?P<path>(?:~|/|\.{1,2}/)?[^\s'\"`]+?\.(?:h5|hdf5|parquet|csv))",
    re.IGNORECASE,
)


class ClioAgent(dspy.Module):
    """CLIO Agent -- Router + Chat Agent + Expert dispatch.

    Architecture:
        User Query -> Router (fast SLM, Literal output)
            -> "data" -> DataExpert (ReAct + HDF5 MCP tools)
            -> "analysis" -> AnalysisExpert (ReAct + Parquet MCP tools)
            -> "visualization" -> VisualizationExpert (ReAct + matplotlib tools)
            -> "chat" -> ChatAgent (CoT conversational)
            -> "none" -> Out-of-scope fallback message

    Attributes:
        router: DSPy ChainOfThought module with RouterSignature
        chat_agent: DSPy ChainOfThought module with ChatAgentSignature
        data_expert: DataExpert instance with ReAct + MCP tools
        analysis_expert: AnalysisExpert instance with ReAct + Parquet tools
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
        """Initialize ClioAgent with Router + ChatAgent + all experts.

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

        if self._provider_config.provider == "lm_studio":
            # LM Studio: fetch available models for dynamic selection
            available_models = fetch_lm_studio_models()
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
            print(f"[ClioAgent] Main/Router model: {main_model}")
            print(f"[ClioAgent] Expert model: {expert_model}")

        # Router: ChainOfThought with Literal output on fast model
        self._router_lm = create_router_lm(self._provider_config)
        self.router = dspy.ChainOfThought(RouterSignature)

        # Chat Agent: ChainOfThought for conversation
        self.chat_agent = dspy.ChainOfThought(ChatAgentSignature)

        # DataExpert: ReAct with real HDF5 MCP tools
        self.data_expert = DataExpert(arc_memory=self.arc)

        # AnalysisExpert: ReAct with real Parquet MCP tools
        self.analysis_expert = AnalysisExpert(arc_memory=self.arc)

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
                ],
                specialization="data_analysis",
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

    def forward(self, question: str, session_id: str = "default") -> dspy.Prediction:
        """Process question through Router -> Expert/Chat dispatch.

        Flow:
            1. Retrieve context from ARC Memory
            2. Route query using Router with fast model (Literal output)
            3. Load dataset profiles from ARC for expert context
            4. Dispatch to expert or ChatAgent
            5. Store routing decision + metrics + conversation in ARC
            6. Return response with selected_expert field

        Args:
            question: User's question or request
            session_id: Session identifier for conversation tracking

        Returns:
            dspy.Prediction with answer, selected_expert, session_id,
            duration_ms, arc_stats, lsm_stats
        """
        start_time = time.time()

        # Step 1: Retrieve context from ARC Memory
        session_context = self._get_session_context(question, session_id)

        # Step 2: Route query. Obvious file/tool requests use deterministic
        # routing first so demos do not depend on local model parsing latency.
        success = False
        error_msg = None
        selected = "chat"  # default fallback

        heuristic_selected = self._route_with_heuristics(question)
        if heuristic_selected:
            if self.verbose:
                print(f"[Router] heuristic route: {heuristic_selected}")
            selected = heuristic_selected
        else:
            try:
                with dspy.context(lm=self._router_lm):
                    routing = self.router(question=question)
                selected = routing.selected_expert
            except Exception as e:
                if self.verbose:
                    routing_err = RoutingError(
                        message=f"Router failed, falling back to chat: {e}",
                        details={"original_error": str(e)},
                    )
                    print(f"[Router] {routing_err.to_dict()}")
                selected = "chat"

        if self.verbose:
            print(f"[Router] {question[:50]}... -> {selected}")

        # Step 3: Load dataset profiles for file context
        file_context = self._get_file_context(session_id)

        # Step 4: Dispatch to expert or chat agent
        answer = ""
        expert_result = None
        error_info = None
        try:
            if selected == "data":
                expert_result = self._direct_tool_answer(selected, question, file_context)
                if expert_result is None:
                    expert_result = self.data_expert(question=question, file_context=file_context)
                answer = (
                    f"{expert_result.analysis}\n\nRecommendations:\n{expert_result.recommendations}"
                )
            elif selected == "analysis":
                expert_result = self._direct_tool_answer(selected, question, file_context)
                if expert_result is None:
                    expert_result = self.analysis_expert(
                        question=question, file_context=file_context
                    )
                answer = (
                    f"{expert_result.analysis}\n\nRecommendations:\n{expert_result.recommendations}"
                )
            elif selected == "visualization":
                expert_result = self._direct_tool_answer(selected, question, file_context)
                if expert_result is None:
                    expert_result = self.visualization_expert(
                        question=question, file_context=file_context
                    )
                answer = f"Visualization: {expert_result.visualization_description}\n\nFile: {expert_result.file_path}"
            elif selected == "none":
                answer = (
                    "I'm CLIO, specialized in scientific data. I can help with "
                    "HDF5/Parquet analysis, statistical analysis, and data visualization. "
                    "Could you rephrase your question in terms of data analysis?"
                )
            else:  # "chat"
                expert_result = self.chat_agent(question=question, session_context=session_context)
                answer = self._coerce_text(expert_result.answer)
            success = True
        except Exception as e:
            success = False
            expert_err = ExpertError(
                message=f"The {selected} expert encountered an issue processing your request.",
                details={"expert": selected, "original_error": str(e)},
            )
            error_info = expert_err.to_dict()
            error_msg = str(e)
            if self.verbose:
                print(f"[ClioAgent] Error in {selected} dispatch: {e}")
            answer = (
                f"I encountered an issue with the {selected} expert. "
                "Please try rephrasing your question or try again later."
            )

        # Step 4b: Store tier-2 expert invocation for optimizer training data
        expert_duration_ms = (time.time() - start_time) * 1000
        if selected in ("data", "analysis", "visualization"):
            self._store_expert_invocation(
                question=question,
                file_context=file_context,
                selected=selected,
                session_id=session_id,
                expert_result=expert_result,
                success=success,
                error_msg=error_msg,
                duration_ms=expert_duration_ms,
            )

        # Step 5: Store conversation + routing decision + metrics in ARC
        # Conversation must be stored first so routing decision can append to it
        duration_ms = (time.time() - start_time) * 1000
        self._store_conversation(question, answer, session_id)
        self._store_routing_decision(question, selected, session_id)
        self._store_metrics(question, session_id, selected, duration_ms, success, error_msg)

        return dspy.Prediction(
            answer=answer,
            selected_expert=selected,
            session_id=session_id,
            duration_ms=duration_ms,
            arc_stats=self.arc.get_cache_stats(),
            lsm_stats=self.lsm.get_stats(),
            error_info=error_info,
        )

    @staticmethod
    def _route_with_heuristics(question: str) -> str | None:
        """Return a deterministic route for obvious MVP/demo intents."""
        q = question.lower()

        if any(word in q for word in ("plot", "chart", "graph", "histogram", "scatter")):
            return "visualization"
        if any(token in q for token in (".h5", ".hdf5", "hdf5", "chunking", "compression")):
            return "data"
        if any(
            token in q for token in (".parquet", "parquet", "schema", "statistics", "null count")
        ):
            return "analysis"
        if any(token in q for token in (".csv", "csv")):
            return "analysis"
        if q.strip() in {"hi", "hello", "hey"} or "who are you" in q:
            return "chat"
        return None

    def _direct_tool_answer(
        self, selected: str, question: str, file_context: str
    ) -> dspy.Prediction | None:
        """Use deterministic local tools for explicit file-path questions."""
        if selected == "data":
            return self._direct_hdf5_answer(question, file_context)
        if selected == "analysis":
            return self._direct_parquet_answer(question, file_context) or self._direct_csv_answer(
                question, file_context
            )
        if selected == "visualization":
            return self._direct_visualization_answer(question, file_context)
        return None

    def _direct_hdf5_answer(self, question: str, file_context: str) -> dspy.Prediction | None:
        """Inspect an explicit HDF5 path without relying on ReAct planning."""
        paths = self._extract_file_paths(question, file_context, {".h5", ".hdf5"})
        if not paths:
            return None

        filepath = paths[0]
        from clio_agent.tools.servers.hdf5_server import analyze_file, list_datasets

        overview = self._call_tool_function(analyze_file, str(filepath))
        datasets = self._call_tool_function(list_datasets, str(filepath))

        if "error" in overview:
            return dspy.Prediction(
                analysis=(
                    f"Could not inspect HDF5 file {filepath}: "
                    f"{self._format_tool_error(overview['error'])}"
                ),
                recommendations="Verify the path exists and that the file is readable HDF5.",
            )

        dataset_rows = datasets.get("datasets", []) if isinstance(datasets, dict) else []
        dataset_lines = [
            f"- {d['path']}: shape={d['shape']}, dtype={d['dtype']}, "
            f"size={self._format_bytes(d['size_bytes'])}"
            for d in dataset_rows[:12]
        ]
        if len(dataset_rows) > 12:
            dataset_lines.append(f"- ... {len(dataset_rows) - 12} more datasets")

        comp_summary = overview.get("compression_summary", {})
        total = overview.get("total_datasets", len(dataset_rows))
        compressed = comp_summary.get("compressed_datasets", 0)
        uncompressed = comp_summary.get("uncompressed_datasets", 0)
        ratio = comp_summary.get("overall_ratio")

        analysis = (
            f"Inspected HDF5 file {filepath}. It contains {total} datasets "
            f"and {overview.get('total_groups', 0)} groups.\n"
            + ("\n".join(dataset_lines) if dataset_lines else "No datasets were found.")
            + "\n\n"
            f"Compression summary: {compressed} compressed, {uncompressed} uncompressed."
        )
        if ratio is not None:
            analysis += f" Overall raw-to-stored ratio is about {ratio}x."

        if uncompressed:
            recommendations = (
                "Compression is partially configured. Review uncompressed numeric datasets and "
                "consider chunked gzip/lzf compression when read patterns tolerate it. Keep chunk "
                "sizes near 1 MiB as a starting point, then tune for row, column, or random access."
            )
        else:
            recommendations = (
                "Compression coverage looks reasonable. Validate chunk shapes against the dominant "
                "read pattern before changing the file layout."
            )

        return dspy.Prediction(analysis=analysis, recommendations=recommendations)

    def _direct_parquet_answer(self, question: str, file_context: str) -> dspy.Prediction | None:
        """Inspect an explicit Parquet path without relying on ReAct planning."""
        paths = self._extract_file_paths(question, file_context, {".parquet"})
        if not paths:
            return None

        filepath = paths[0]
        from clio_agent.tools.servers.parquet_server import analyze_schema, compute_statistics

        schema = self._call_tool_function(analyze_schema, str(filepath))
        if "error" in schema:
            return dspy.Prediction(
                analysis=(
                    f"Could not inspect Parquet file {filepath}: "
                    f"{self._format_tool_error(schema['error'])}"
                ),
                recommendations="Verify the path exists and that the file is readable Parquet.",
            )

        columns = schema.get("columns", [])
        column_lines = [
            f"- {c['name']}: {c['type']}, nullable={c['nullable']}" for c in columns[:12]
        ]
        if len(columns) > 12:
            column_lines.append(f"- ... {len(columns) - 12} more columns")

        stats_lines = []
        q_lower = question.lower()
        for col in columns[:4]:
            name = col["name"]
            if name.lower() not in q_lower and "stat" not in q_lower:
                continue
            stats = self._call_tool_function(compute_statistics, str(filepath), name)
            if "error" not in stats:
                stats_bits = [
                    f"{k}={stats[k]}"
                    for k in ("min", "max", "mean", "null_count", "unique_count")
                    if k in stats
                ]
                stats_lines.append(f"{name}: " + ", ".join(stats_bits))

        analysis = (
            f"Inspected Parquet file {filepath}. It has {schema.get('num_rows')} rows, "
            f"{schema.get('num_columns')} columns, and {schema.get('num_row_groups')} row groups.\n"
            + "\n".join(column_lines)
        )
        if stats_lines:
            analysis += "\n\nColumn statistics:\n" + "\n".join(stats_lines)

        recommendations = (
            "Use the schema and row group count to decide whether the file needs repartitioning. "
            "For analysis questions, compute statistics on the specific columns involved instead "
            "of scanning every column."
        )

        return dspy.Prediction(analysis=analysis, recommendations=recommendations)

    def _direct_csv_answer(self, question: str, file_context: str) -> dspy.Prediction | None:
        """Inspect an explicit CSV path without relying on chat fallback."""
        paths = self._extract_file_paths(question, file_context, {".csv"})
        if not paths:
            return None

        filepath = paths[0]
        try:
            import pyarrow.csv as pcsv

            from clio_agent.tools.file_policy import FilePolicyError, validate_read_path

            safe_path = validate_read_path(str(filepath))
            table = pcsv.read_csv(safe_path)
        except FilePolicyError as exc:
            return dspy.Prediction(
                analysis=(
                    f"Could not inspect CSV file {filepath}: "
                    f"{self._format_tool_error(exc.to_result()['error'])}"
                ),
                recommendations="Move the file under an allowed root or adjust CLIO_ALLOWED_ROOTS.",
            )
        except Exception as exc:
            return dspy.Prediction(
                analysis=f"Could not inspect CSV file {filepath}: {exc}",
                recommendations="Verify the path exists and that the file is readable CSV.",
            )

        column_lines = []
        for field in table.schema:
            null_count = table.column(field.name).null_count
            column_lines.append(f"- {field.name}: {field.type}, nulls={null_count}")

        analysis = (
            f"Inspected CSV file {safe_path}. It has {table.num_rows} rows and "
            f"{table.num_columns} columns.\n"
            + ("\n".join(column_lines) if column_lines else "No columns were found.")
        )
        recommendations = (
            "CSV is readable in local mode. For repeated analysis or larger files, convert to "
            "Parquet so schema, compression, and column statistics are cheaper to inspect."
        )

        return dspy.Prediction(analysis=analysis, recommendations=recommendations)

    def _direct_visualization_answer(
        self, question: str, file_context: str
    ) -> dspy.Prediction | None:
        """Create a deterministic summary plot for explicit tabular file paths."""
        paths = self._extract_file_paths(question, file_context, {".parquet", ".csv"})
        if not paths:
            return None

        filepath = paths[0]
        output_dir = Path(".clio_agent") / "charts"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"summary_{filepath.stem}.png"

        from clio_agent.experts.visualization_expert import plot_summary

        chart_path = plot_summary(str(filepath), output_path=str(output_path))
        if chart_path.startswith("Error:"):
            return dspy.Prediction(
                visualization_description=f"Could not create visualization: {chart_path}",
                file_path="",
            )

        return dspy.Prediction(
            visualization_description=(
                f"Created a summary dashboard for {filepath} with data types, null counts, "
                "numeric distributions, and correlations where available."
            ),
            file_path=chart_path,
        )

    @staticmethod
    def _extract_file_paths(question: str, file_context: str, suffixes: set[str]) -> list[Path]:
        """Extract existing file paths with one of the requested suffixes."""
        paths: list[Path] = []
        seen: set[str] = set()

        def add_matches(text: str, *, include_missing: bool) -> None:
            for match in _FILE_PATH_RE.finditer(text):
                raw = match.group("path").rstrip(".,;:)]}")
                path = Path(raw).expanduser()
                if path.suffix.lower() not in suffixes:
                    continue
                if not path.is_absolute():
                    path = path.resolve()
                if not include_missing and not path.exists():
                    continue
                key = str(path)
                if key not in seen:
                    paths.append(path)
                    seen.add(key)

        add_matches(question, include_missing=True)
        add_matches(file_context, include_missing=False)
        return paths

    @staticmethod
    def _format_bytes(size: int) -> str:
        """Format byte counts for compact terminal/API answers."""
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.1f} {unit}"
            value /= 1024

    @staticmethod
    def _call_tool_function(tool: Any, *args: Any, **kwargs: Any) -> Any:
        """Call either a FastMCP FunctionTool or a plain Python helper."""
        fn = getattr(tool, "fn", tool)
        return fn(*args, **kwargs)

    @staticmethod
    def _format_tool_error(error: Any) -> str:
        """Format structured tool errors for user-facing direct answers."""
        if isinstance(error, dict):
            message = error.get("message") or str(error)
            next_action = error.get("next_action")
            if next_action:
                return f"{message} Next action: {next_action}"
            return str(message)
        return str(error)

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
            tier: Agent tier for token budget (1=router/2K, 2=expert/4K)

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

    def _get_file_context(self, session_id: str) -> str:
        """Load dataset profiles from ARC for expert file context.

        Args:
            session_id: Session identifier

        Returns:
            JSON string of dataset profiles, or empty string if none.
        """
        try:
            profiles = self.arc.get_session_profiles(session_id)
            if profiles:
                return json.dumps(
                    [
                        {
                            "filepath": p.filepath,
                            "schema": p.schema_info,
                            "stats": p.statistics,
                        }
                        for p in profiles
                    ]
                )
        except Exception:
            pass
        return ""

    def _store_routing_decision(self, question: str, selected: str, session_id: str) -> None:
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
                capabilities_needed=[],
                selected_agent=selected,
                reasoning="Literal router",
                confidence=1.0,
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
        invocation_id = str(uuid.uuid4())
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
            tools_called=[],
            nanoagents_spawned=[],
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
                tools_called=[],
                nanoagents_spawned=[],
                performance={"success": success, "duration_ms": duration_ms},
                storage_tier="warm",
            )
            self.arc.store_invocation(invocation)
        except Exception as e:
            if self.verbose:
                print(f"[ClioAgent] Warning: Failed to store expert invocation: {e}")

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
