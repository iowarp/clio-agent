"""
ClioAgent - Main Agent Module

Router + ChatAgent + Expert dispatch architecture.

Architecture:
    User Query -> Router (fast SLM, Literal output)
        -> "data" -> DataExpert (ReAct + HDF5 MCP tools)
        -> "chat" -> ChatAgent (CoT conversational)

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

import time
import uuid
from typing import Any, Dict, List

import dspy

from clio_agent.arc.lsm import LSMTree
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.retrieval import ContextRetriever
from clio_agent.arc.schema import (
    Conversation,
    Invocation,
    Message,
)
from clio_agent.config import (
    RouterLMConfig,
    configure_dspy_router_lm_studio,
    fetch_lm_studio_models,
    select_models_for_agents,
)
from clio_agent.experts import DataExpert
from clio_agent.registry.registry import AgentCapability, AgentRegistry
from clio_agent.signatures.main_agent_sig import ChatAgentSignature, RouterSignature


class ClioAgent(dspy.Module):
    """CLIO Agent -- Router + Chat Agent + Expert dispatch.

    Architecture:
        User Query -> Router (fast SLM, Literal output)
            -> "data" -> DataExpert (ReAct + HDF5 MCP tools)
            -> "chat" -> ChatAgent (CoT conversational)

    Attributes:
        router: DSPy ChainOfThought module with RouterSignature
        chat_agent: DSPy ChainOfThought module with ChatAgentSignature
        data_expert: DataExpert instance with ReAct + MCP tools
        arc: ARC Memory instance
        context_retriever: Context retrieval module
        registry: Agent registry for discovery
        lsm: LSM Tree for metrics storage

    Example:
        >>> agent = ClioAgent()
        >>> result = agent(question="Optimize my HDF5 file", session_id="session-123")
        >>> print(result.answer)
        >>> print(result.selected_expert)  # "data" or "chat"
    """

    def __init__(self, verbose: bool = False, data_dir: str = ".clio_agent"):
        """Initialize ClioAgent with Router + ChatAgent + DataExpert.

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

        # DataExpert: ReAct with real MCP tools
        self.data_expert = DataExpert(arc_memory=self.arc)

        # Register in registry
        self.registry.register_agent(
            "data",
            self.data_expert,
            AgentCapability(
                keywords=['hdf5', 'compression', 'chunking', 'data', 'io', 'parquet'],
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

        if self.verbose:
            print(f"[ClioAgent] Registered {self.registry.get_agent_count()} experts")
            print(f"[ClioAgent] ARC Memory initialized at {data_dir}/arc")
            print(f"[ClioAgent] LSM Tree initialized at {data_dir}/arc/lsm")

    def forward(self, question: str, session_id: str = "default") -> dspy.Prediction:
        """Process question through Router -> Expert/Chat dispatch.

        Flow:
            1. Retrieve context from ARC Memory
            2. Route query using Router with fast model (Literal output)
            3. Dispatch to DataExpert or ChatAgent
            4. Store metrics + conversation in ARC
            5. Return response with selected_expert field

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
            selected = routing.selected_expert  # "data" or "chat"
        except Exception as e:
            if self.verbose:
                print(f"[Router] Error: {e}, falling back to chat")
            selected = "chat"

        if self.verbose:
            print(f"[Router] {question[:50]}... -> {selected}")

        # Step 3: Dispatch to expert or chat agent
        answer = ""
        try:
            if selected == "data":
                result = self.data_expert(question=question, file_context="")
                answer = f"{result.analysis}\n\nRecommendations:\n{result.recommendations}"
            else:
                result = self.chat_agent(
                    question=question, session_context=session_context
                )
                answer = result.answer
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

        # Step 4: Store metrics + conversation in ARC
        duration_ms = (time.time() - start_time) * 1000
        self._store_metrics(question, session_id, selected, duration_ms, success, error_msg)
        self._store_conversation(question, answer, session_id)

        return dspy.Prediction(
            answer=answer,
            selected_expert=selected,
            session_id=session_id,
            duration_ms=duration_ms,
            arc_stats=self.arc.get_cache_stats(),
            lsm_stats=self.lsm.get_stats(),
        )

    def _get_session_context(self, question: str, session_id: str) -> str:
        """Retrieve session context from ARC Memory.

        Args:
            question: User's current question
            session_id: Session identifier

        Returns:
            Compiled context string or "No prior context"
        """
        session_context = "No prior context"
        try:
            arc_context = self.context_retriever.retrieve_context_for_query(
                query=question,
                session_id=session_id,
                max_history=5,
            )

            if self.verbose:
                print(
                    f"[ClioAgent] Retrieved {len(arc_context.learned_patterns)} "
                    "context patterns from ARC"
                )

            if arc_context.learned_patterns:
                context_parts = []
                for p in arc_context.learned_patterns:
                    if hasattr(p, 'pattern_data') and isinstance(p.pattern_data, dict):
                        for key, value in p.pattern_data.items():
                            if value and isinstance(value, str):
                                context_parts.append(f"{key}: {value}")
                if context_parts:
                    session_context = "; ".join(context_parts[:5])
        except Exception as e:
            if self.verbose:
                print(f"[ClioAgent] Warning: Failed to retrieve context from ARC: {e}")

        return session_context

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
            tier=1 if selected_expert == "chat" else 2,
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
