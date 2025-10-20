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
Uses DSPy for intelligent routing to expert capabilities.

Architecture:
    User Question
        ↓
    ClaudIO (DSPy ChainOfThought)
        └─ Routes to DataExpert
        ↓
    DataExpert (DSPy ReAct)
        ├─ Reasons about approach
        ├─ Calls MCP tools (HDF5, ADIOS, Parquet)
        └─ Returns answer

Key Principles:
- Program, don't prompt: Uses DSPy signatures
- Tool-augmented expert: ReAct with MCP tools
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

import dspy
from typing import Dict, Any, Optional, List
import sys
import os
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent  # src/claudio/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

# Import signatures
from claudio.signatures.main_agent_sig import MainAgentSignature
from claudio.signatures.expert_sig import DataExpertSignature

# Import experts
from claudio.experts import (
    get_all_experts,
    get_expert_capabilities,
    DataExpert,
)

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
    configure_dspy_lm_studio,
    configure_dspy_router_lm_studio,
    configure_dspy_reasoner_lm_studio,
    fetch_lm_studio_models,
    select_models_for_agents,
    LMStudioConfig,
    RouterLMConfig,
    ReasonerLMConfig
)


# ============================================================================
# CLAUDIO MAIN AGENT
# ============================================================================

class ClaudIO(dspy.Module):
    """ClaudIO - Multi-agent system for conversational data I/O optimization.

    Complex DSPy module with conversational chat, CoT reasoning, and expert integration.
    Uses two LM instances: one for main conversational agent, one for expert analysis.

    Architecture:
        - Main Agent: Conversational, uses CoT for natural flow and routing
        - DataExpert: ReAct with tools for detailed analysis
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

    def __init__(self, verbose: bool = False):
        """Initialize ClaudIO multi-agent system.

        Args:
            verbose: If True, print reasoning and decisions
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
        main_config = LMStudioConfig(model=main_model)  # Main agent
        router_config = RouterLMConfig(model=main_model)  # Deterministic routing (same as main)
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

        # Conversation manager for history
        self.conversation_manager = ConversationManager(max_length=10)

        # Configuration
        self.verbose = verbose

    def add_to_history(self, role: str, content: str):
        """Add message to history."""
        self.conversation_manager.add_message(role, content)

    def get_history(self) -> dspy.History:
        """Get history as dspy.History."""
        return self.conversation_manager.get_history()

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

    def forward(self, question: str, history: Optional[dspy.History] = None, context: Optional[str] = None) -> dspy.Prediction:
        """Route question to appropriate expert and get answer.

        This is the main entry point for ClaudIO. It demonstrates DSPy's
        declarative approach to multi-agent systems.

        Flow:
            1. ChainOfThought analyzes question → selects expert
            2. Get expert instance from registry
            3. Expert (ReAct) processes question → calls tools → returns answer
            4. ClaudIO assembles full response with traces

        Args:
            question: User's question or request
            history: Conversation history for context-aware responses
            context: Optional additional context

        Returns:
            dspy.Prediction with fields:
                - routing_reasoning: Step-by-step why expert was selected
                - selected_expert: Expert ID ('data' or 'none')
                - answer: Conversational response with expert analysis
                - [expert_reasoning]: Expert's CoT (if available)
                - [recommendations]: Structured recommendations (if available)
                - [trajectory]: Tool calls and observations (if available)

        Example:
            >>> agent = ClaudIO()
            >>> history = dspy.History(messages=[])
            >>> result = agent(
            ...     question="My HDF5 file is 100GB, how do I optimize it?",
            ...     history=history,
            ...     context="Using parallel HDF5 on 64 cores"
            ... )
            >>> print(result.selected_expert)  # "data"
            >>> print(result.routing_reasoning)  # Step-by-step reasoning
            >>> print(result.answer)  # Conversational response
        """

        # STEP 1: Swarm Routing and Expert (Parallel Execution with History)
        # ==================================================================
        # Use history buffer for context
        current_history = self.get_history() if not history else history

        # Run routing and expert in parallel for faster processing
        def route_task():
            return self.router(
                question=question,
                available_experts=self._format_capabilities(),
                history=current_history
            )

        def expert_task():
            try:
                expert = self.experts.get("data", self.data_expert)
                return expert(question=question, file_context="", history=current_history)
            except Exception as e:
                # Fallback if expert fails
                return dspy.Prediction(
                    analysis=f"Error in expert: {str(e)}. Fallback: For data I/O questions, consider HDF5 compression and chunking strategies.",
                    recommendations="1. Use gzip compression for HDF5.\n2. Choose chunk sizes based on access patterns.\n3. Enable parallel I/O for large datasets."
                )

        # Parallel execution
        results = self.parallel([(route_task, {}), (expert_task, {})])
        routing, expert_result = results

        if self.verbose:
            print(f"\n[ClaudIO] Routing reasoning: {routing.reasoning}")
            print(f"\n[ClaudIO] Selected expert: {routing.selected_expert}")

        # STEP 2: Normalize and Validate Expert Selection
        # =================================================
        expert_id = routing.selected_expert.lower().strip()

        # Handle multi-word expert IDs (e.g., "data expert" → "data")
        if ' ' in expert_id:
            expert_id = expert_id.split()[0]

        # Handle 'none' for general chat
        if expert_id == "none":
            if self.verbose:
                print(f"[ClaudIO] General chat detected, responding directly")
            answer = "Hi! I'm ClaudIO, your friendly guide to scientific data I/O optimization. I can help with HDF5, ADIOS, Parquet, and more. What would you like to know?"
            return dspy.Prediction(
                routing_reasoning=routing.reasoning,
                selected_expert="none",
                answer=answer
            )

        # Fallback logic if expert not found
        if expert_id not in self.experts:
            if self.verbose:
                print(f"[ClaudIO] Unknown expert '{expert_id}', using 'data' as fallback")
            expert_id = "data"

        # STEP 3: Synthesize Response from Parallel Results
        # ================================================
        # Expert result from parallel execution
        try:
            # Data expert returns: analysis + recommendations
            answer = f"{expert_result.analysis}\n\n**Recommendations:**\n{expert_result.recommendations}"

            # Extract additional metadata
            extra_fields = {}

            # Reasoning trace (if ChainOfThought used)
            if hasattr(expert_result, 'reasoning'):
                extra_fields['expert_reasoning'] = expert_result.reasoning

            # Tool trajectory (if ReAct used)
            if hasattr(expert_result, 'trajectory'):
                extra_fields['trajectory'] = expert_result.trajectory
                # Count tool calls
                extra_fields['num_tool_calls'] = len(expert_result.trajectory) if expert_result.trajectory else 0

        except Exception as e:
            # Graceful error handling
            if self.verbose:
                print(f"[ClaudIO] Error in expert result: {e}")
                import traceback
                traceback.print_exc()

            answer = (
                f"I encountered an error while processing your request with the {expert_id} expert.\n\n"
                f"Error: {str(e)}\n\n"
                f"This might be due to:\n"
                f"- MCP tools not being available\n"
                f"- Expert configuration issues\n"
                f"- Malformed input\n\n"
                f"Please check the logs for details."
            )
            extra_fields = {
                'error': str(e),
                'error_type': type(e).__name__
            }

        # STEP 5: Assemble Response and Update History
        # ============================================
        response = dspy.Prediction(
            routing_reasoning=routing.reasoning,
            selected_expert=expert_id,
            answer=answer,
            **extra_fields
        )

        # Update history buffer
        self.add_to_history("user", question)
        self.add_to_history("assistant", answer)

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
