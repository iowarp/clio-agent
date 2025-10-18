#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
Warpio DSPy POC - Orchestrator

Routes questions to appropriate experts using DSPy ChainOfThought.
Run with: uv run orchestrator.py
"""

import dspy
from typing import Dict, Any, List
import sys
import os

# Import experts (inline for UV script)
sys.path.insert(0, os.path.dirname(__file__))
from experts import get_all_experts, get_expert_capabilities


# ============================================================================
# ORCHESTRATOR SIGNATURE
# ============================================================================

class OrchestratorSignature(dspy.Signature):
    """Route user questions to the most appropriate expert."""

    question: str = dspy.InputField(desc="User's question or request")
    available_experts: str = dspy.InputField(desc="List of available experts and their capabilities")

    reasoning: str = dspy.OutputField(desc="Analysis of which expert is best suited")
    selected_expert: str = dspy.OutputField(desc="Name of selected expert: general, code, or data")


# ============================================================================
# ORCHESTRATOR MODULE
# ============================================================================

class WarpioOrchestrator(dspy.Module):
    """Main orchestrator that routes questions to experts."""

    def __init__(self):
        super().__init__()

        # Router uses ChainOfThought for reasoning about expert selection
        self.router = dspy.ChainOfThought(OrchestratorSignature)

        # Load experts
        self.experts = get_all_experts()
        self.expert_capabilities = get_expert_capabilities()

    def _format_capabilities(self) -> str:
        """Format expert capabilities for the router."""
        lines = []
        for expert_id, caps in self.expert_capabilities.items():
            lines.append(f"- {expert_id}: {caps['description']}")
            lines.append(f"  Keywords: {', '.join(caps['keywords'])}")
        return "\n".join(lines)

    def forward(self, question: str) -> dspy.Prediction:
        """Route question to appropriate expert and get answer."""

        # Step 1: Route to expert
        routing = self.router(
            question=question,
            available_experts=self._format_capabilities()
        )

        # Step 2: Execute with selected expert
        expert_id = routing.selected_expert.lower().strip()

        # Fallback to general if expert not found
        if expert_id not in self.experts:
            expert_id = "general"

        # Get expert response
        expert = self.experts[expert_id]

        try:
            if expert_id == "code":
                result = expert(question=question, language="python")
                answer = f"{result.explanation}\n\nCode:\n{result.code}"
            elif expert_id == "data":
                result = expert(question=question, data_context="")
                answer = f"{result.analysis}\n\nRecommendations:\n{result.recommendations}"
            else:  # general
                result = expert(question=question, context="")
                answer = result.answer

        except Exception as e:
            answer = f"Error executing expert: {str(e)}"

        return dspy.Prediction(
            routing_reasoning=routing.reasoning,
            selected_expert=expert_id,
            answer=answer
        )


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("Warpio DSPy POC - Orchestrator Test")
    print("=" * 50)

    # Import config
    from config import setup_dspy

    try:
        print("\nInitializing with LM Studio...")
        lm = setup_dspy(use_lm_studio=True)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("Make sure LM Studio is running at http://127.0.0.1:1234")
        sys.exit(1)

    # Create orchestrator
    orchestrator = WarpioOrchestrator()

    # Test questions
    test_questions = [
        "What is machine learning?",
        "Write a Python function to calculate fibonacci numbers",
        "How should I analyze a dataset with missing values?"
    ]

    for i, question in enumerate(test_questions, 1):
        print(f"\n{i}. Question: {question}")
        print("-" * 50)

        result = orchestrator(question=question)

        print(f"Routing: {result.routing_reasoning}")
        print(f"Expert: {result.selected_expert}")
        print(f"\nAnswer: {result.answer[:300]}...")

    print("\n✅ Orchestrator working!")
