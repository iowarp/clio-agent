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
    RouterLMConfig,
    configure_dspy_router_lm_studio,
    fetch_lm_studio_models,
    select_models_for_agents,
)
from clio_agent.experts import AnalysisExpert, DataExpert, VisualizationExpert
from clio_agent.optimizer.instrumentation import _extract_output
from clio_agent.registry.registry import AgentCapability, AgentRegistry
from clio_agent.signatures.main_agent_sig import ChatAgentSignature, RouterSignature


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

        # Fetch available models from LM Studio
        available_models = fetch_lm_studio_models()
        main_model, expert_model = select_models_for_agents(available_models)

        if self.verbose:
            print(f"[ClioAgent] Main/Router model: {main_model}")
            print(f"[ClioAgent] Expert model: {expert_model}")

        # Router: ChainOfThought with Literal output on fast model
        router_config = RouterLMConfig(model=main_model)
        self._router_lm = configure_dspy_router_lm_studio(router_config)
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
                keywords=['hdf5', 'compression', 'chunking', 'data', 'io'],
                description='Data I/O optimization expert with HDF5 tools',
                tools=[
                    'hdf5_list_datasets',
                    'hdf5_analyze_dataset',
                    'hdf5_check_compression',
                    'hdf5_optimize_chunking',
                    'hdf5_analyze_file',
                ],
                specialization='data_io'
            )
        )

        self.registry.register_agent(
            "analysis",
            self.analysis_expert,
            AgentCapability(
                keywords=['parquet', 'statistics', 'schema', 'profiling', 'analysis', 'data quality'],
                description='Statistical analysis and data profiling expert with Parquet tools',
                tools=[
                    'parquet_analyze_schema',
                    'parquet_query_data',
                    'parquet_compute_statistics',
                ],
                specialization='data_analysis'
            )
        )

        self.registry.register_agent(
            "visualization",
            self.visualization_expert,
            AgentCapability(
                keywords=['plot', 'chart', 'histogram', 'scatter', 'visualization', 'graph'],
                description='Scientific data visualization expert with matplotlib tools',
                tools=[
                    'plot_histogram',
                    'plot_bar_chart',
                    'plot_scatter',
                    'plot_summary',
                ],
                specialization='data_visualization'
            )
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
                            print(f"[ClioAgent] Warning: Could not load variant for {agent_id}: {e}")
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

        # Step 2: Route query using Router with fast model
        success = False
        error_msg = None
        selected = "chat"  # default fallback

        try:
            with dspy.context(lm=self._router_lm):
                routing = self.router(question=question)
            selected = routing.selected_expert
        except Exception as e:
            if self.verbose:
                print(f"[Router] Error: {e}, falling back to chat")
            selected = "chat"

        if self.verbose:
            print(f"[Router] {question[:50]}... -> {selected}")

        # Step 3: Load dataset profiles for file context
        file_context = self._get_file_context(session_id)

        # Step 4: Dispatch to expert or chat agent
        answer = ""
        expert_result = None
        try:
            if selected == "data":
                expert_result = self.data_expert(question=question, file_context=file_context)
                answer = f"{expert_result.analysis}\n\nRecommendations:\n{expert_result.recommendations}"
            elif selected == "analysis":
                expert_result = self.analysis_expert(question=question, file_context=file_context)
                answer = f"{expert_result.analysis}\n\nRecommendations:\n{expert_result.recommendations}"
            elif selected == "visualization":
                expert_result = self.visualization_expert(question=question, file_context=file_context)
                answer = f"Visualization: {expert_result.visualization_description}\n\nFile: {expert_result.file_path}"
            elif selected == "none":
                answer = (
                    "I'm CLIO, specialized in scientific data. I can help with "
                    "HDF5/Parquet analysis, statistical analysis, and data visualization. "
                    "Could you rephrase your question in terms of data analysis?"
                )
            else:  # "chat"
                expert_result = self.chat_agent(
                    question=question, session_context=session_context
                )
                answer = expert_result.answer
            success = True
        except Exception as e:
            success = False
            error_msg = str(e)
            if self.verbose:
                print(f"[ClioAgent] Error in {selected} dispatch: {e}")
                import traceback
                traceback.print_exc()
            answer = (
                f"I encountered an error processing your question: {e}. "
                "For data I/O questions, consider HDF5 compression and chunking strategies."
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
        )

    def _get_session_context(
        self, question: str, session_id: str, tier: int = 2
    ) -> str:
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
                        if hasattr(p, 'pattern_data') and isinstance(p.pattern_data, dict):
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
                return json.dumps([
                    {
                        "filepath": p.filepath,
                        "schema": p.schema_info,
                        "stats": p.statistics,
                    }
                    for p in profiles
                ])
        except Exception:
            pass
        return ""

    def _store_routing_decision(
        self, question: str, selected: str, session_id: str
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
        """Clean shutdown of ClioAgent. Closes LSM Tree."""
        if self.verbose:
            print("[ClioAgent] Shutting down...")

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
