"""
ClioAgent - Main Agent Module

The primary ClioAgent agent that uses DSPy ChainOfThought with subagents.
Plan 03 will restructure this into Router + ChatAgent with ReAct.

Architecture:
    User Question
        |
    ClioAgent (ChainOfThought)
        |- Retrieves context from ARC Memory
        |- Stores invocation + conversation in ARC
        |
    Returns answer

Usage:
    >>> from clio_agent import ClioAgent
    >>> from clio_agent.config import setup_dspy
    >>>
    >>> lm = setup_dspy()
    >>> agent = ClioAgent()
    >>> result = agent(question="How do I optimize HDF5 files?")
    >>> print(result.answer)
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
    ToolCall,
)
from clio_agent.config import (
    LMStudioConfig,
    configure_dspy_lm_studio,
    fetch_lm_studio_models,
    select_models_for_agents,
)
from clio_agent.experts import DataExpert
from clio_agent.registry.registry import AgentCapability, AgentRegistry
from clio_agent.signatures.main_agent_sig import MainAgentSignature


class ClioAgent(dspy.Module):
    """ClioAgent - ChainOfThought-based orchestration for conversational data I/O optimization.

    Uses DSPy ChainOfThought with ARC Memory for context. Plan 03 will convert to
    Router + ReAct pattern with experts as tools.

    Attributes:
        agent: DSPy ChainOfThought module
        data_expert: DataExpert instance
        arc: ARC Memory instance
        context_retriever: Context retrieval module
        registry: Agent registry for discovery
        lsm: LSM Tree for metrics storage

    Example:
        >>> agent = ClioAgent()
        >>> result = agent(question="Optimize my HDF5 file", session_id="session-123")
        >>> print(result.answer)
    """

    def __init__(self, verbose: bool = False, data_dir: str = ".clio_agent"):
        """Initialize ClioAgent.

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
        if self.verbose:
            print(f"Available models from LM Studio: {available_models}")

        # Select models for main and expert
        main_model, expert_model = select_models_for_agents(available_models)
        if self.verbose:
            print(f"Selected main model: {main_model}")
            print(f"Selected expert model: {expert_model}")

        # Initialize experts with ARC Memory
        self.data_expert = DataExpert(arc_memory=self.arc)

        # Register experts in registry (for /experts command)
        self.registry.register_agent(
            "data",
            self.data_expert,
            AgentCapability(
                keywords=['hdf5', 'adios', 'parquet', 'data', 'io', 'compression', 'chunking'],
                description='Data I/O optimization expert',
                tools=['analyze_hdf5', 'optimize_chunks'],
                specialization='data_io'
            )
        )

        # Main agent uses ChainOfThought
        main_config = LMStudioConfig(model=main_model)

        with dspy.context(lm=configure_dspy_lm_studio(main_config)):
            self.agent = dspy.ChainOfThought(
                MainAgentSignature,
                n=3
            )

        if self.verbose:
            print(f"[ClioAgent] Registered {self.registry.get_agent_count()} experts in registry")
            print(f"[ClioAgent] ARC Memory initialized at {data_dir}/arc")
            print(f"[ClioAgent] LSM Tree initialized at {data_dir}/arc/lsm")

    def forward(self, question: str, session_id: str = "default") -> dspy.Prediction:
        """Process question using ChainOfThought with ARC Memory.

        Flow:
            1. Retrieve context from ARC Memory
            2. ChainOfThought processes question
            3. Store invocation metrics in LSM Tree
            4. Store conversation in ARC Memory
            5. Return response

        Args:
            question: User's question or request
            session_id: Session identifier for conversation tracking

        Returns:
            dspy.Prediction with answer, trajectory, session_id, duration_ms, arc_stats, lsm_stats
        """
        start_time = time.time()

        # STEP 1: Retrieve context from ARC Memory
        session_context = "No prior context"
        try:
            arc_context = self.context_retriever.retrieve_context_for_query(
                query=question,
                session_id=session_id,
                max_history=5
            )

            if self.verbose:
                print(f"\n[ClioAgent] Retrieved {len(arc_context.learned_patterns)} context patterns from ARC")

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

        # STEP 2: ChainOfThought agent
        success = False
        error_msg = None
        result = None

        try:
            result = self.agent(
                question=question,
                session_context=session_context
            )
            success = True
        except Exception as e:
            success = False
            error_msg = str(e)
            if self.verbose:
                print(f"[ClioAgent] Error in execution: {e}")
                import traceback
                traceback.print_exc()
            result = dspy.Prediction(
                answer=f"I encountered an error processing your question: {str(e)}. For data I/O questions, consider HDF5 compression and chunking strategies.",
                trajectory=[]
            )

        # STEP 3: Store invocation metrics in LSM Tree
        duration_ms = (time.time() - start_time) * 1000

        self.lsm.write(
            timestamp=time.time(),
            metric={
                "session_id": session_id,
                "query": question,
                "duration_ms": duration_ms,
                "success": success,
                "error": error_msg,
                "num_tool_calls": len(result.trajectory) if hasattr(result, 'trajectory') and result.trajectory else 0
            }
        )

        # STEP 4: Store invocation in ARC Memory
        invocation_id = str(uuid.uuid4())
        tool_calls: list[ToolCall] = []

        if hasattr(result, 'trajectory') and result.trajectory:
            for step in result.trajectory:
                if isinstance(step, dict):
                    tool_calls.append(ToolCall(
                        tool=step.get('tool', 'unknown'),
                        params=step.get('params', {}),
                        result=step.get('result', {}),
                        duration_ms=step.get('duration_ms', 0),
                        cached=step.get('cached', False)
                    ))

        invocation = Invocation(
            trace_id=invocation_id,
            session_id=session_id,
            parent_trace_id=None,
            agent_id="main",
            tier=1,
            source="native",
            started_at=start_time,
            completed_at=time.time(),
            duration_ms=duration_ms,
            status="success" if success else "failure",
            input={"query": question, "session_context": session_context},
            output={
                "answer": result.answer if hasattr(result, 'answer') else str(result),
                "error": error_msg
            },
            tools_called=tool_calls,
            nanoagents_spawned=[],
            performance={"success": success, "duration_ms": duration_ms},
            storage_tier="warm"
        )
        self.arc.store_invocation(invocation)

        if self.verbose:
            print(f"[ClioAgent] Stored invocation {invocation_id} in ARC")

        # STEP 5: Store conversation in ARC Memory
        answer = result.answer if hasattr(result, 'answer') else str(result)
        self._store_conversation(question, answer, session_id)

        if self.verbose:
            print(f"[ClioAgent] Stored conversation in ARC for session {session_id}")

        # STEP 6: Assemble response
        total_duration_ms = (time.time() - start_time) * 1000

        response = dspy.Prediction(
            answer=answer,
            trajectory=result.trajectory if hasattr(result, 'trajectory') else [],
            session_id=session_id,
            duration_ms=total_duration_ms,
            arc_stats=self.arc.get_cache_stats(),
            lsm_stats=self.lsm.get_stats()
        )

        return response

    def _store_conversation(self, question: str, answer: str, session_id: str) -> None:
        """Store conversation in ARC Memory."""
        current_time = time.time()
        msg_id_user = str(uuid.uuid4())
        msg_id_assistant = str(uuid.uuid4())

        user_msg = Message(
            message_id=msg_id_user,
            role="user",
            content=question,
            timestamp=current_time,
            metadata={"source": "clio_agent_main"}
        )

        assistant_msg = Message(
            message_id=msg_id_assistant,
            role="assistant",
            content=answer,
            timestamp=current_time,
            metadata={"agent": "main"}
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
                storage_tier="warm"
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
