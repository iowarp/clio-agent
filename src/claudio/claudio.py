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
from typing import Dict, Any, Optional
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

# Import experts
from claudio.experts import (
    get_all_experts,
    get_expert_capabilities,
    DataExpert,
)


# ============================================================================
# CLAUDIO MAIN AGENT
# ============================================================================

class ClaudIO(dspy.Module):
    """ClaudIO - Main agent for scientific data I/O optimization.

    The primary interface between users and data I/O expertise.
    Routes questions to DataExpert using DSPy ChainOfThought reasoning.

    Attributes:
        router: DSPy ChainOfThought module for expert selection
        experts: Dictionary containing DataExpert instance
        expert_capabilities: Metadata about DataExpert capabilities

    Example:
        >>> agent = ClaudIO()
        >>> result = agent(question="Optimize my HDF5 file")
        >>> # Routes to DataExpert and returns optimized answer
    """

    def __init__(self, verbose: bool = False):
        """Initialize ClaudIO agent.

        Args:
            verbose: If True, print routing decisions and expert reasoning
        """
        super().__init__()

        # Router uses ChainOfThought for expert selection reasoning
        # (preserved for future multi-expert expansion)
        self.router = dspy.ChainOfThought(MainAgentSignature)

        # Load data expert only
        self.experts = get_all_experts()
        self.expert_capabilities = get_expert_capabilities()

        # Configuration
        self.verbose = verbose

    def _format_capabilities(self) -> str:
        """Format expert capabilities for the router.

        Returns:
            Formatted string describing DataExpert capabilities

        Example output:
            - data: HDF5, ADIOS, Parquet optimization expert
              Keywords: hdf5, adios, parquet, compression, chunking
        """
        lines = []
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

    def forward(self, question: str, context: Optional[str] = None) -> dspy.Prediction:
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
            context: Optional additional context

        Returns:
            dspy.Prediction with fields:
                - routing_reasoning: Why this expert was selected
                - selected_expert: Expert ID that was used
                - answer: Expert's response
                - [expert_reasoning]: Expert's thought process (if available)
                - [recommendations]: Structured recommendations (if available)
                - [tool_calls]: MCP tools used (if available)

        Example:
            >>> agent = ClaudIO()
            >>> result = agent(
            ...     question="My HDF5 file is 100GB, how do I optimize it?",
            ...     context="Using parallel HDF5 on 64 cores"
            ... )
            >>> print(result.selected_expert)  # "data"
            >>> print(result.routing_reasoning)  # ChainOfThought reasoning
            >>> print(result.answer)  # Expert's answer (from ReAct)
        """

        # STEP 1: Expert Selection (DSPy ChainOfThought)
        # ================================================
        # Uses ChainOfThought to reason about which expert is best suited
        routing = self.router(
            question=question,
            available_experts=self._format_capabilities()
        )

        if self.verbose:
            print(f"\n[ClaudIO] Routing reasoning: {routing.reasoning}")
            print(f"\n[ClaudIO] Selected expert: {routing.selected_expert}")

        # STEP 2: Normalize and Validate Expert Selection
        # =================================================
        expert_id = routing.selected_expert.lower().strip()

        # Handle multi-word expert IDs (e.g., "data expert" → "data")
        if ' ' in expert_id:
            expert_id = expert_id.split()[0]

        # Fallback logic if expert not found
        if expert_id not in self.experts:
            if self.verbose:
                print(f"[ClaudIO] Unknown expert '{expert_id}', using 'data' as fallback")
            expert_id = "data"

        # STEP 3: Get Expert and Prepare Context
        # ========================================
        expert = self.experts[expert_id]
        expert_context = context or self._get_expert_context(expert_id)

        # STEP 4: Execute Expert with Expert-Specific Fields
        # ====================================================
        # DataExpert has specific input/output structure
        try:
            # Call data expert with appropriate context field name
            result = expert(question=question, file_context=expert_context)
            # Data expert returns: analysis + recommendations
            answer = f"{result.analysis}\n\n**Recommendations:**\n{result.recommendations}"

            # Extract additional metadata
            extra_fields = {}

            # Reasoning trace (if ChainOfThought used)
            if hasattr(result, 'reasoning'):
                extra_fields['expert_reasoning'] = result.reasoning

            # Tool trajectory (if ReAct used)
            if hasattr(result, 'trajectory'):
                extra_fields['trajectory'] = result.trajectory
                # Count tool calls
                extra_fields['num_tool_calls'] = len(result.trajectory) if result.trajectory else 0

        except Exception as e:
            # Graceful error handling
            if self.verbose:
                print(f"[ClaudIO] Error executing {expert_id}: {e}")
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

        # STEP 5: Assemble Response
        # ==========================
        return dspy.Prediction(
            routing_reasoning=routing.reasoning,
            selected_expert=expert_id,
            answer=answer,
            **extra_fields
        )


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

        result = agent(question=question)

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
