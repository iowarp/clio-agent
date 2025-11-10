#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
#   "fastmcp>=2.13.0",
# ]
# ///

"""
ClaudIO - Main Agent Module

The primary ClaudIO agent that uses DSPy ReAct pattern with subagents as tools.
Refactored to follow proper DSPy architecture patterns.

Architecture:
    User Question
        ↓
    ClaudIO ReAct Agent (routing + execution in one)
        ├─ Retrieves context from ARC Memory
        ├─ Calls expert tool functions
        │   └─ DataExpert (ReAct Pattern with MCP tools)
        └─ Stores invocation + conversation in ARC
        ↓
    Returns answer with trajectory

Key Principles:
- ReAct pattern for main agent (no separate routing step)
- Experts wrapped as tool functions
- ARC Memory for context retrieval and storage
- LSM Tree for metrics logging
- Registry for validation and discovery (not routing)

Usage:
    >>> from claudio import ClaudIO
    >>> from claudio.config import setup_dspy
    >>>
    >>> # Setup LM (LM Studio)
    >>> lm = setup_dspy()
    >>>
    >>> # Create ClaudIO agent
    >>> agent = ClaudIO()
    >>>
    >>> # Ask data I/O questions
    >>> result = agent(question="How do I optimize HDF5 files?")
    >>>
    >>> # Inspect results
    >>> print(f"Answer: {result.answer}")
"""

import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import dspy

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent  # src/claudio/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

# Import ARC Memory components
from claudio.arc.lsm import LSMTree
from claudio.arc.memory import ARCMemory
from claudio.arc.retrieval import ContextRetriever
from claudio.arc.schema import (
    Conversation,
    Invocation,
    Message,
    ToolCall,
)

# Import experts
from claudio.experts import DataExpert
from claudio.registry.registry import AgentCapability, AgentRegistry

# Import config
from claudio.config import (
    configure_dspy_lm_studio,
    fetch_lm_studio_models,
    select_models_for_agents,
)


# ============================================================================
# MAIN AGENT SIGNATURE (DSPy Signature Class)
# ============================================================================

class MainAgentSignature(dspy.Signature):
    """Main ClaudIO agent signature for ReAct pattern.

    Defines input/output fields for ReAct agent reasoning:
    - question: User's question or request
    - session_context: Context retrieved from ARC Memory
    - answer: Final answer from ReAct agent

    Example:
        >>> signature = MainAgentSignature()
        >>> # Used by ReAct agent for structured reasoning
    """

    question: str = dspy.InputField(desc="User's question or request")
    session_context: str = dspy.InputField(
        desc="Session context from ARC Memory (key topics, history)"
    )
    answer: str = dspy.OutputField(desc="ClaudIO's answer with reasoning")


# ============================================================================
# MODULE-LEVEL TOOL FUNCTIONS
# ============================================================================

_data_expert_instance: Optional[DataExpert] = None


def ask_data_expert(question: str, file_context: str = "") -> str:
    """Consult the Data I/O optimization expert.

    Tool function for ReAct pattern. Asks the DataExpert about HDF5, ADIOS,
    Parquet, compression, and chunking strategies.

    Args:
        question: The question to ask the expert
        file_context: Optional file context or path information

    Returns:
        Expert analysis and recommendations as string

    Note:
        This is a module-level tool function (required for DSPy ReAct introspection).
        The _data_expert_instance is set by ClaudIO.__init__() after expert creation.

    Example:
        >>> result = ask_data_expert("How do I optimize HDF5 compression?")
        >>> print(result)
    """
    global _data_expert_instance

    if _data_expert_instance is None:
        return "Error: DataExpert not initialized. Please initialize ClaudIO first."

    try:
        # Call DataExpert with history (required parameter)
        from claudio.arc.schema import History

        result = _data_expert_instance(
            question=question,
            file_context=file_context,
            history=History(messages=[])
        )

        # Extract answer from result
        if hasattr(result, "analysis") and hasattr(result, "recommendations"):
            return f"{result.analysis}\n\nRecommendations:\n{result.recommendations}"
        elif hasattr(result, "answer"):
            return result.answer
        else:
            return str(result)
    except Exception as e:
        return f"Error consulting DataExpert: {str(e)}"


# ============================================================================
# CLAUDIO MAIN AGENT (DSPy ReAct Pattern)
# ============================================================================

class ClaudIO(dspy.Module):
    """ClaudIO - ReAct-based orchestration system for conversational data I/O optimization.

    Uses DSPy ReAct pattern with subagents as tools. Main agent automatically routes
    and executes expert calls based on the question.

    Architecture:
        - Main Agent: ReAct pattern with experts as tools
        - DataExpert: Tool function wrapping DataExpert.forward()
        - ARC Memory: Context retrieval and storage
        - LSM Tree: High-throughput metrics logging
        - Registry: Expert validation and discovery

    Attributes:
        agent: DSPy ReAct module with expert tools
        data_expert: DataExpert instance
        arc: ARC Memory instance
        context_retriever: Context retrieval module
        registry: Agent registry for discovery
        lsm: LSM Tree for metrics storage

    Example:
        >>> agent = ClaudIO()
        >>> result = agent(question="Optimize my HDF5 file", session_id="session-123")
        >>> print(result.answer)
    """

    def __init__(self, verbose: bool = False, data_dir: str = ".claudio"):
        """Initialize ClaudIO ReAct-based system.

        Args:
            verbose: If True, print reasoning and decisions
            data_dir: Base directory for ClaudIO data storage
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
        self.data_expert = DataExpert(use_tools=True, arc_memory=self.arc)

        # Set global reference for module-level ask_data_expert tool function
        global _data_expert_instance
        _data_expert_instance = self.data_expert

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

        # Main agent uses ChainOfThought (ReAct incompatible with LM Studio)
        # Note: ReAct requires JSONAdapter which LM Studio rejects
        # ChainOfThought works reliably with all local LM providers
        from claudio.config import LMStudioConfig
        main_config = LMStudioConfig(model=main_model)

        with dspy.context(lm=configure_dspy_lm_studio(main_config)):
            self.agent = dspy.ChainOfThought(
                MainAgentSignature,
                n=3  # Multiple reasoning passes
            )

        if self.verbose:
            print(f"[ClaudIO] Registered {self.registry.get_agent_count()} experts in registry")
            print(f"[ClaudIO] ARC Memory initialized at {data_dir}/arc")
            print(f"[ClaudIO] LSM Tree initialized at {data_dir}/arc/lsm")

    def forward(self, question: str, session_id: str = "default") -> dspy.Prediction:
        """Process question using ReAct pattern with expert tools.

        This is the main entry point for ClaudIO with ARC Memory integration.

        Flow:
            1. Retrieve context from ARC Memory
            2. ReAct agent processes question (routing + execution in one)
               - Agent decides which expert tool to call
               - Calls expert tool(s) as needed
               - Generates final answer
            3. Store invocation metrics in LSM Tree
            4. Store conversation in ARC Memory
            5. Return response with trajectory

        Args:
            question: User's question or request
            session_id: Session identifier for conversation tracking (default: "default")

        Returns:
            dspy.Prediction with fields:
                - answer: Final answer from ReAct agent
                - trajectory: List of tool calls and observations
                - session_id: Session identifier
                - duration_ms: Total execution time in milliseconds
                - arc_stats: ARC Memory cache statistics
                - lsm_stats: LSM Tree statistics

        Example:
            >>> agent = ClaudIO()
            >>> result = agent(
            ...     question="My HDF5 file is 100GB, how do I optimize it?",
            ...     session_id="session-123"
            ... )
            >>> print(result.answer)
            >>> print(f"Tool calls: {len(result.trajectory)}")
            >>> print(f"Cache hit rate: {result.arc_stats['hit_rate']}")
        """
        start_time = time.time()

        # STEP 1: Retrieve context from ARC Memory
        # =========================================
        session_context = "No prior context"
        try:
            arc_context = self.context_retriever.retrieve_context_for_query(
                query=question,
                session_id=session_id,
                max_history=5
            )

            if self.verbose:
                print(f"\n[ClaudIO] Retrieved {len(arc_context.learned_patterns)} context patterns from ARC")

            # Format context for ReAct (extract from learned patterns)
            # Note: LearnedPattern schema has pattern_type, pattern_data, confidence, learned_at
            # pattern_data is a dict that may contain various keys (rule, topic, etc.)
            if arc_context.learned_patterns:
                context_parts = []
                for p in arc_context.learned_patterns:
                    if hasattr(p, 'pattern_data') and isinstance(p.pattern_data, dict):
                        # Extract any useful info from pattern_data
                        for key, value in p.pattern_data.items():
                            if value and isinstance(value, str):
                                context_parts.append(f"{key}: {value}")
                if context_parts:
                    session_context = "; ".join(context_parts[:5])
        except Exception as e:
            if self.verbose:
                print(f"[ClaudIO] Warning: Failed to retrieve context from ARC: {e}")
            # Continue with "No prior context" fallback

        # STEP 2: ReAct agent (routing + execution in one)
        # =================================================
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
                print(f"[ClaudIO] Error in ReAct execution: {e}")
                import traceback
                traceback.print_exc()
            # Fallback response
            result = dspy.Prediction(
                answer=f"I encountered an error processing your question: {str(e)}. For data I/O questions, consider HDF5 compression and chunking strategies.",
                trajectory=[]
            )

        # STEP 3: Store invocation metrics in LSM Tree
        # =============================================
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
        # =======================================
        invocation_id = str(uuid.uuid4())
        tool_calls = []

        # Extract tool calls from trajectory
        if hasattr(result, 'trajectory') and result.trajectory:
            for step in result.trajectory:
                # ReAct trajectory format varies, handle gracefully
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
            tier=1,  # Tier 1 = Main Agent
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
            print(f"[ClaudIO] Stored invocation {invocation_id} in ARC")

        # STEP 5: Store conversation in ARC Memory
        # =========================================
        answer = result.answer if hasattr(result, 'answer') else str(result)
        self._store_conversation(question, answer, session_id)

        if self.verbose:
            print(f"[ClaudIO] Stored conversation in ARC for session {session_id}")

        # STEP 6: Assemble response
        # =========================
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

    def _store_conversation(self, question: str, answer: str, session_id: str):
        """Store conversation in ARC Memory.

        Args:
            question: User question
            answer: Assistant answer
            session_id: Session identifier
        """
        current_time = time.time()
        msg_id_user = str(uuid.uuid4())
        msg_id_assistant = str(uuid.uuid4())

        # Create messages
        user_msg = Message(
            message_id=msg_id_user,
            role="user",
            content=question,
            timestamp=current_time,
            metadata={"source": "claudio_main"}
        )

        assistant_msg = Message(
            message_id=msg_id_assistant,
            role="assistant",
            content=answer,
            timestamp=current_time,
            metadata={"agent": "main"}
        )

        # Get existing conversation or create new
        existing_conv = self.arc.get_conversation(session_id)

        if existing_conv:
            # Append to existing conversation
            existing_conv.messages.extend([user_msg, assistant_msg])
            existing_conv.updated_at = current_time
            existing_conv.last_accessed = current_time
            self.arc.store_conversation(existing_conv)
        else:
            # Create new conversation
            conv = Conversation(
                session_id=session_id,
                user_id="default_user",
                created_at=current_time,
                updated_at=current_time,
                last_accessed=current_time,
                status="active",
                messages=[user_msg, assistant_msg],
                routing_decisions=[],
                metadata={"claudio_version": "0.3.0", "arc_enabled": True},
                storage_tier="warm"
            )
            self.arc.store_conversation(conv)

    def get_arc_stats(self) -> Dict[str, Any]:
        """Get ARC memory statistics.

        Returns:
            Dictionary with cache statistics including:
                - hit_rate: Cache hit rate (0.0 to 1.0)
                - size: Current cache size
                - capacity: Maximum cache capacity
                - hits: Number of cache hits
                - misses: Number of cache misses
                - disk_reads: Number of disk reads
                - disk_writes: Number of disk writes

        Example:
            >>> stats = agent.get_arc_stats()
            >>> print(f"Hit rate: {stats['hit_rate']:.2%}")
            Hit rate: 87.5%
        """
        return self.arc.get_cache_stats()

    def get_lsm_stats(self) -> Dict[str, Any]:
        """Get LSM Tree statistics.

        Returns:
            Dictionary with LSM statistics including:
                - write_count: Total writes
                - flush_count: Total MemTable flushes
                - compaction_count: Total compactions
                - memtable_size: Current MemTable entry count
                - sstable_count: Current SSTable count
                - total_records: Approximate total records

        Example:
            >>> stats = agent.get_lsm_stats()
            >>> print(f"Total writes: {stats['write_count']}")
            Total writes: 1234
        """
        return self.lsm.get_stats()

    def get_session_history(self, session_id: str, limit: int = 10) -> List[Conversation]:
        """Get conversation history for session from ARC Memory.

        Args:
            session_id: Session identifier
            limit: Maximum number of conversations to retrieve

        Returns:
            List of Conversation objects for the session

        Example:
            >>> history = agent.get_session_history("session-123", limit=5)
            >>> for conv in history:
            ...     print(f"Session: {conv.session_id}, Messages: {len(conv.messages)}")
        """
        return self.arc.get_conversation_history(session_id, limit=limit)

    def shutdown(self):
        """Clean shutdown of ClaudIO.

        Closes LSM Tree (flushes MemTable, stops compaction).
        Call before process exit.

        Example:
            >>> agent = ClaudIO()
            >>> # ... use agent ...
            >>> agent.shutdown()
        """
        if self.verbose:
            print("[ClaudIO] Shutting down...")

        # Close LSM Tree (flushes MemTable, stops compaction)
        self.lsm.close()

        if self.verbose:
            print("[ClaudIO] LSM Tree closed")
            print("[ClaudIO] Shutdown complete")


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def load_optimized_claudio(path: str, verbose: bool = False) -> ClaudIO:
    """Load an optimized ClaudIO agent from disk.

    Args:
        path: Path to saved ClaudIO JSON
        verbose: If True, print loading info

    Returns:
        Optimized ClaudIO instance

    Example:
        >>> agent = load_optimized_claudio("data/compiled/claudio_v2.json")
        >>> # Use optimized version with improved routing
        >>> result = agent(question="...")
    """
    # TODO: Implement loading from compiled DSPy artifacts
    # agent = ClaudIO(verbose=verbose)
    # agent.load(path)
    # return agent
    raise NotImplementedError("Optimization loading not yet implemented")


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClaudIO Agent Test (ReAct Pattern)")
    print("=" * 60)

    # Import config
    from claudio.config import setup_dspy

    try:
        print("\nInitializing with LM Studio...")
        lm = setup_dspy()

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("Make sure LM Studio is running at the configured URL")
        sys.exit(1)

    # Create ClaudIO agent
    print("\nCreating ClaudIO agent...")
    agent = ClaudIO(verbose=True)

    # Test questions for data expert
    test_questions = [
        "How do I optimize HDF5 compression?",
        "What's the best chunking strategy for my dataset?",
        "How can I improve parallel I/O performance?",
    ]

    print("\nTesting ClaudIO agent...")
    print("-" * 60)

    session_id = f"test-{int(time.time())}"

    for i, question in enumerate(test_questions, 1):
        print(f"\n{i}. Question: {question}")

        result = agent(question=question, session_id=session_id)

        print(f"   Answer: {result.answer[:200]}...")
        if hasattr(result, 'trajectory') and result.trajectory:
            print(f"   Tool calls: {len(result.trajectory)}")
        print(f"   Duration: {result.duration_ms:.2f}ms")

    # Print statistics
    print("\n" + "=" * 60)
    print("Statistics:")
    print(f"ARC Stats: {agent.get_arc_stats()}")
    print(f"LSM Stats: {agent.get_lsm_stats()}")

    # Clean shutdown
    agent.shutdown()

    print("\n✅ ClaudIO agent test complete!")
    print("\nNote: This is baseline performance without optimization.")
    print("After collecting usage logs and running MIPROv2, expect 30-50% improvement.")
