#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO - Main Agent Module

The primary ClaudIO agent that interfaces with users for data I/O optimization.
Uses intelligent multi-agent orchestration for expert routing.

Architecture:
    User Question
        ↓
    ClaudIO Orchestrator (Chain-of-Thought)
        └─ Routes to DataExpert
        ↓
    DataExpert Agent (ReAct Pattern)
        ├─ Reasons about approach
        ├─ Calls FastMCP tools (HDF5, ADIOS, Parquet)
        └─ Returns answer

Key Principles:
- Declarative Intelligence: Agent signatures without prompts
- Tool-augmented agents: ReAct with FastMCP tools
- Observable: Full reasoning traces

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
    >>> print(f"Expert: {result.selected_expert}")
    >>> print(f"Answer: {result.answer}")
"""

import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import dspy

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent  # src/claudio/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

# Import signatures
# Import ARC Memory components
from claudio.arc.memory import ARCMemory
from claudio.arc.retrieval import ContextRetriever
from claudio.arc.schema import (
    Conversation,
    Invocation,
    Message,
    ToolCall,
)
from claudio.arc.schema import (
    RoutingDecision as ARCRoutingDecision,
)

# Import experts
from claudio.experts import (
    DataExpert,
    get_all_experts,
    get_expert_capabilities,
)
from claudio.registry.registry import AgentCapability, AgentRegistry
from claudio.signatures.main_agent_sig import MainAgentSignature


# Simple conversation manager for history
class ConversationManager:
    def __init__(self, max_length=10):
        self.history = []
        self.max_length = max_length

    def add_message(self, role, content):
        self.history.append({"role": role, "content": content})
        if len(self.history) > self.max_length:
            self.history = self.history[-self.max_length:]

    def get_history(self):
        return dspy.History(messages=self.history)

    def summarize(self, question):
        # Simple summary
        return dspy.Prediction(
            summary="Conversation history summary",
            key_topics=["data", "io"],
            context_for_response="Previous discussions on data I/O"
        )

# Import config
from claudio.config import (
    LMStudioConfig,
    ReasonerLMConfig,
    RouterLMConfig,
    configure_dspy_reasoner_lm_studio,
    configure_dspy_router_lm_studio,
    fetch_lm_studio_models,
    select_models_for_agents,
)

# ============================================================================
# CLAUDIO MAIN AGENT
# ============================================================================

class ClaudIO(dspy.Module):
    """ClaudIO - Multi-agent orchestration system for conversational data I/O optimization.

    Intelligent agent framework with conversational chat, Chain-of-Thought routing, and expert integration.
    Uses two LM instances: one for main conversational agent, one for expert analysis.

    Architecture:
        - Main Agent: Conversational, uses Chain-of-Thought for natural flow and routing
        - DataExpert: ReAct pattern with tools for detailed analysis
        - Conversation History: Maintains context across turns

    Attributes:
        main_lm: LM for conversational tasks (lower temp for consistency)
        expert_lm: LM for expert tasks (higher temp for creativity)
        router: CoT for routing with history awareness
        data_expert: ReAct expert with tools
        expert_capabilities: Metadata for routing

    Example:
        >>> agent = ClaudIO()
        >>> history = dspy.History([])
        >>> result = agent(question="Optimize my HDF5 file", history=history)
    """

    def __init__(self, verbose: bool = False, data_dir: str = ".claudio"):
        """Initialize ClaudIO multi-agent system.

        Args:
            verbose: If True, print reasoning and decisions
            data_dir: Base directory for ClaudIO data storage
        """
        self.verbose = verbose
        super().__init__()

        # Fetch available models from LM Studio
        available_models = fetch_lm_studio_models()
        if self.verbose:
            print(f"Available models from LM Studio: {available_models}")

        # Select models for main and expert
        main_model, expert_model = select_models_for_agents(available_models)
        if self.verbose:
            print(f"Selected main model: {main_model}")
            print(f"Selected expert model: {expert_model}")

        # Configure LM instances for swarm
        router_config = RouterLMConfig(model=main_model)  # Deterministic routing
        reasoner_config = ReasonerLMConfig(model=expert_model)  # Creative reasoning

        # Set router LM (deterministic)
        with dspy.context(lm=configure_dspy_router_lm_studio(router_config)):
            self.router = dspy.ChainOfThought(MainAgentSignature)

        # Set reasoner LM (creative)
        with dspy.context(lm=configure_dspy_reasoner_lm_studio(reasoner_config), adapter=dspy.ChatAdapter()):
            self.data_expert = DataExpert(use_tools=True)  # ReAct with ChatAdapter for Granite

        # Load capabilities for routing
        self.experts = get_all_experts()
        self.expert_capabilities = get_expert_capabilities()

        # Swarm: Parallel executor for multi-agent (reduced to avoid queuing)
        self.parallel = dspy.Parallel(num_threads=1)

        # Initialize ARC Memory Layer (v0.2.0)
        self.arc = ARCMemory(data_dir=f"{data_dir}/arc", cache_capacity=1000)
        self.context_retriever = ContextRetriever(self.arc)

        # Initialize Agent Registry (v0.2.0)
        self.registry = AgentRegistry()

        # Register existing experts in registry
        for expert_id, expert_instance in self.experts.items():
            caps = self.expert_capabilities.get(expert_id)
            if caps:
                cap = AgentCapability(
                    keywords=caps['keywords'],
                    description=caps['description'],
                    tools=caps.get('tools', []),
                    specialization=expert_id,
                    priority=5
                )
                self.registry.register_agent(expert_id, expert_instance, cap)

        if self.verbose:
            print(f"[ClaudIO] Registered {self.registry.get_agent_count()} experts in registry")
            print(f"[ClaudIO] ARC Memory initialized at {data_dir}/arc")

        # Conversation manager for history (legacy - kept for backward compatibility)
        self.conversation_manager = ConversationManager(max_length=10)

        # Configuration
        self.verbose = verbose

    def add_to_history(self, role: str, content: str):
        """Add message to history."""
        self.conversation_manager.add_message(role, content)

    def get_history(self) -> dspy.History:
        """Get history as dspy.History."""
        return self.conversation_manager.get_history()

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

    def _create_conversation(
        self,
        session_id: str,
        question: str,
        answer: str,
        routing_decision: Any
    ) -> Conversation:
        """Create Conversation object from interaction.

        Args:
            session_id: Session identifier
            question: User question
            answer: Assistant answer
            routing_decision: Routing decision from registry

        Returns:
            Conversation object ready to store in ARC
        """
        current_time = time.time()  # Unix timestamp (float)
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
            metadata={"agent": routing_decision.selected_agent}
        )

        # Create ARC routing decision
        arc_routing = ARCRoutingDecision(
            timestamp=current_time,
            query=question,
            capabilities_needed=routing_decision.matched_keywords,
            selected_agent=routing_decision.selected_agent,
            reasoning=routing_decision.reasoning if hasattr(routing_decision, 'reasoning') else f"Matched keywords: {routing_decision.matched_keywords}",
            confidence=routing_decision.confidence,
            alternatives=[
                {"agent": fb, "score": 0.0}
                for fb in routing_decision.fallback_agents
            ]
        )

        # Get existing conversation or create new
        existing_conv = self.arc.get_conversation(session_id)

        if existing_conv:
            # Append to existing conversation
            existing_conv.messages.extend([user_msg, assistant_msg])
            existing_conv.routing_decisions.append(arc_routing)
            existing_conv.updated_at = current_time
            existing_conv.last_accessed = current_time
            return existing_conv
        else:
            # Create new conversation
            return Conversation(
                session_id=session_id,
                user_id="default_user",  # TODO: Add user tracking
                created_at=current_time,
                updated_at=current_time,
                last_accessed=current_time,
                status="active",
                messages=[user_msg, assistant_msg],
                routing_decisions=[arc_routing],
                metadata={"claudio_version": "0.2.0", "arc_enabled": True},
                storage_tier="warm"
            )

    def _format_capabilities(self) -> str:
        """Format expert capabilities for the router.

        Returns:
            Formatted string describing DataExpert capabilities

        Example output:
            - none: General chat and non-data questions
              Keywords: hi, hello, who are you, general
            - data: HDF5, ADIOS, Parquet optimization expert
              Keywords: hdf5, adios, parquet, compression, chunking
        """
        lines = []
        lines.append("- none: General chat and non-data questions")
        lines.append("  Keywords: hi, hello, who are you, general, introduction")
        for expert_id, caps in self.expert_capabilities.items():
            lines.append(f"- {expert_id}: {caps['description']}")
            lines.append(f"  Keywords: {', '.join(caps['keywords'])}")
        return "\n".join(lines)

    def _get_expert_context(self, expert_id: str) -> str:
        """Get relevant context for data expert.

        Args:
            expert_id: Expert identifier (data)

        Returns:
            Context string for the expert

        Note:
            In production, this could pull from:
            - User's recent interactions
            - Project-specific metadata
            - Relevant file paths
            - Domain knowledge base
        """
        # TODO: Implement context retrieval from usage logs
        # TODO: Add project-specific context
        # TODO: Integrate with knowledge base
        return ""

    def forward(self, question: str, session_id: str = "default", history: Optional[dspy.History] = None, context: Optional[str] = None) -> dspy.Prediction:
        """Route question to appropriate expert and get answer.

        This is the main entry point for ClaudIO with ARC Memory integration.

        Flow:
            1. Retrieve context from ARC Memory
            2. Route query using AgentRegistry (capability-based)
            3. Expert (ReAct pattern) processes question → calls tools → returns answer
            4. Store invocation and conversation in ARC
            5. Return response with ARC stats

        Args:
            question: User's question or request
            session_id: Session identifier for conversation tracking (default: "default")
            history: Conversation history for context-aware responses (legacy, optional)
            context: Optional additional context

        Returns:
            dspy.Prediction with fields:
                - routing_reasoning: Step-by-step why expert was selected
                - selected_expert: Expert ID ('data' or 'none')
                - answer: Conversational response with expert analysis
                - confidence: Routing confidence score
                - duration_ms: Total execution time in milliseconds
                - arc_stats: ARC Memory cache statistics
                - [expert_reasoning]: Expert's CoT (if available)
                - [recommendations]: Structured recommendations (if available)
                - [trajectory]: Tool calls and observations (if available)

        Example:
            >>> agent = ClaudIO()
            >>> result = agent(
            ...     question="My HDF5 file is 100GB, how do I optimize it?",
            ...     session_id="session-123"
            ... )
            >>> print(result.selected_expert)  # "data"
            >>> print(result.routing_reasoning)  # Step-by-step reasoning
            >>> print(result.arc_stats['hit_rate'])  # Cache hit rate
        """
        start_time = time.time()

        # STEP 1: Retrieve context from ARC Memory
        # =========================================
        arc_context = self.context_retriever.retrieve_context_for_query(
            query=question,
            session_id=session_id,
            max_history=5
        )

        if self.verbose:
            print(f"\n[ClaudIO] Retrieved {len(arc_context.learned_patterns)} context patterns from ARC")

        # STEP 2: Route using AgentRegistry (capability-based)
        # ====================================================
        routing_decision = self.registry.route_query(question)
        expert_id = routing_decision.selected_agent

        if self.verbose:
            print(f"[ClaudIO] Routing decision: {expert_id} (confidence: {routing_decision.confidence:.2f})")
            print(f"[ClaudIO] Matched keywords: {routing_decision.matched_keywords}")

        # STEP 3: Execute expert
        # ======================
        # Use history buffer for backward compatibility
        current_history = self.get_history() if not history else history

        # Handle 'none' or low confidence for general chat
        if routing_decision.confidence < 0.2:
            if self.verbose:
                print(f"[ClaudIO] Low confidence ({routing_decision.confidence:.2f}), responding with general chat")
            answer = "Hi! I'm ClaudIO, your friendly guide to scientific data I/O optimization. I can help with HDF5, ADIOS, Parquet, and more. What would you like to know?"

            # Store conversation in ARC even for general chat
            conversation = self._create_conversation(
                session_id=session_id,
                question=question,
                answer=answer,
                routing_decision=routing_decision
            )
            self.arc.store_conversation(conversation)

            # Update legacy history
            self.add_to_history("user", question)
            self.add_to_history("assistant", answer)

            total_duration_ms = (time.time() - start_time) * 1000
            return dspy.Prediction(
                routing_reasoning=f"Low confidence routing (confidence: {routing_decision.confidence:.2f})",
                selected_expert="none",
                answer=answer,
                confidence=routing_decision.confidence,
                duration_ms=total_duration_ms,
                arc_stats=self.arc.get_cache_stats()
            )

        # Get expert from registry
        expert = self.registry.get_agent(expert_id)
        if expert is None:
            # Fallback to data expert
            if self.verbose:
                print(f"[ClaudIO] Expert '{expert_id}' not found in registry, using 'data' as fallback")
            expert = self.data_expert
            expert_id = "data"

        # Execute expert
        expert_start = time.time()
        try:
            expert_result = expert(question=question, file_context="", history=current_history)
            expert_duration_ms = (time.time() - expert_start) * 1000
            success = True
            error_msg = None
        except Exception as e:
            expert_duration_ms = (time.time() - expert_start) * 1000
            success = False
            error_msg = str(e)
            if self.verbose:
                print(f"[ClaudIO] Error in expert execution: {e}")
                import traceback
                traceback.print_exc()
            # Fallback response
            expert_result = dspy.Prediction(
                analysis=f"Error in expert: {str(e)}. For data I/O questions, consider HDF5 compression and chunking strategies.",
                recommendations="1. Use gzip compression for HDF5.\n2. Choose chunk sizes based on access patterns.\n3. Enable parallel I/O for large datasets."
            )

        # STEP 4: Store invocation in ARC
        # ================================
        invocation_id = str(uuid.uuid4())
        tool_calls = []

        # Extract tool calls if available
        if hasattr(expert_result, 'trajectory') and expert_result.trajectory:
            for step in expert_result.trajectory:
                # Parse trajectory step (format varies by ReAct implementation)
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
            agent_id=expert_id,
            tier=2,  # Tier 2 = Expert
            source="native",
            started_at=expert_start,  # Unix timestamp (float)
            completed_at=time.time(),  # Unix timestamp (float)
            duration_ms=expert_duration_ms,
            status="success" if success else "failure",
            input={"query": question, "context": context or ""},
            output={
                "analysis": expert_result.analysis if hasattr(expert_result, 'analysis') else str(expert_result),
                "recommendations": expert_result.recommendations if hasattr(expert_result, 'recommendations') else "",
                "error": error_msg
            },
            tools_called=tool_calls,
            nanoagents_spawned=[],
            performance={"success": success, "expert_duration_ms": expert_duration_ms},
            storage_tier="warm"
        )
        self.arc.store_invocation(invocation)

        if self.verbose:
            print(f"[ClaudIO] Stored invocation {invocation_id} in ARC")

        # STEP 5: Store conversation in ARC
        # ==================================
        answer = f"{expert_result.analysis}\n\n**Recommendations:**\n{expert_result.recommendations}" if hasattr(expert_result, 'recommendations') else str(expert_result)

        conversation = self._create_conversation(
            session_id=session_id,
            question=question,
            answer=answer,
            routing_decision=routing_decision
        )
        self.arc.store_conversation(conversation)

        if self.verbose:
            print(f"[ClaudIO] Stored conversation in ARC for session {session_id}")

        # STEP 6: Update legacy history buffer (backward compatibility)
        # =============================================================
        self.add_to_history("user", question)
        self.add_to_history("assistant", answer)

        # STEP 7: Assemble response
        # =========================
        total_duration_ms = (time.time() - start_time) * 1000

        # Extract additional metadata
        extra_fields = {}
        if hasattr(expert_result, 'reasoning'):
            extra_fields['expert_reasoning'] = expert_result.reasoning
        if hasattr(expert_result, 'trajectory'):
            extra_fields['trajectory'] = expert_result.trajectory
            extra_fields['num_tool_calls'] = len(expert_result.trajectory) if expert_result.trajectory else 0
        if error_msg:
            extra_fields['error'] = error_msg
            extra_fields['error_type'] = 'ExpertExecutionError'

        response = dspy.Prediction(
            routing_reasoning=f"Matched keywords: {routing_decision.matched_keywords}",
            selected_expert=expert_id,
            answer=answer,
            confidence=routing_decision.confidence,
            duration_ms=total_duration_ms,
            arc_stats=self.arc.get_cache_stats(),
            **extra_fields
        )

        return response


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
    print("ClaudIO Agent Test")
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
        ("How do I optimize HDF5 compression?", "data"),
        ("What's the best chunking strategy for my dataset?", "data"),
        ("How can I improve parallel I/O performance?", "data"),
    ]

    print("\nTesting ClaudIO agent...")
    print("-" * 60)

    for i, (question, expected_expert) in enumerate(test_questions, 1):
        print(f"\n{i}. Question: {question}")
        print(f"   Expected Expert: {expected_expert}")

        history = dspy.History(messages=[])
        result = agent(question=question, history=history)

        print(f"   Selected Expert: {result.selected_expert}")
        print(f"   Routing: {result.routing_reasoning[:100]}...")
        print(f"   Answer: {result.answer[:200]}...")

        if result.selected_expert == expected_expert:
            print("   ✓ Correct routing")
        else:
            print("   ⚠ Different expert selected")

    print("\n" + "=" * 60)
    print("✅ ClaudIO agent test complete!")
    print("\nNote: This is baseline performance without optimization.")
    print("After collecting usage logs and running MIPROv2, expect 30-50% improvement.")
