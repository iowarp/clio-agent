#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Main Agent Signature

Defines the input/output interface for the ClaudIO main agent.
The agent routes user questions to the most appropriate expert
based on question analysis and expert capabilities.

This signature uses DSPy's declarative programming approach:
- Defines WHAT the agent should do (routing)
- DSPy handles HOW through ChainOfThought reasoning
- No manual prompt engineering required

NOTE: Currently only DataExpert is implemented, but the routing logic
is preserved for future expert expansion.
"""

import dspy
import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/claudio/signatures/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))


class MainAgentSignature(dspy.Signature):
    """Route questions to the appropriate expert for data I/O optimization.

    Input:
        - question: User's question about data files or I/O
        - available_experts: List of experts and their skills
        - history: Conversation history for context

    Output:
        - reasoning: Why this expert is best
        - selected_expert: Expert ID or 'none'
    """

    # Input fields
    question: str = dspy.InputField(desc="User's question about data I/O")
    available_experts: str = dspy.InputField(desc="Available experts and skills")
    history: dspy.History = dspy.InputField(desc="Conversation history")

    # Output fields
    reasoning: str = dspy.OutputField(desc="Why this expert is selected")
    selected_expert: str = dspy.OutputField(desc="Expert ID or 'none'")
    available_experts: str = dspy.InputField(
        desc=(
            "Our team of expert specialists (currently 'data' for HDF5/ADIOS/Parquet). "
            "Format: 'expert_id: friendly description of expertise\\n  Keywords: relevant topics they handle' - I'll match based on keywords and history"
        )
    )
    history: dspy.History = dspy.InputField(
        desc="Our ongoing conversation history - step-by-step context of previous questions, answers, and evolving needs for better matching and natural flow"
    )

    # Output fields
    reasoning: str = dspy.OutputField(
        desc=(
            "Step-by-step analysis: Break down the question, consider history patterns, identify key topics, "
            "and explain why 'data' expert (or direct response) is best. Make it engaging and contextual."
        )
    )
    selected_expert: str = dspy.OutputField(
        desc=(
            "The expert ID ('data' for data I/O tasks, 'none' for general chat). Based on step-by-step reasoning and conversation context."
        )
    )


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("MainAgentSignature Test")
    print("=" * 60)

    # This demonstrates the signature structure
    # In practice, this is used within dspy.ChainOfThought

    print("\nSignature Fields:")
    print("-" * 60)

    print("\nInput Fields:")
    for field_name, field in MainAgentSignature.input_fields.items():
        desc = getattr(field, 'json_schema_extra', {}).get('desc', 'No description') if hasattr(field, 'json_schema_extra') else 'No description'
        print(f"  - {field_name}: {desc}")

    print("\nOutput Fields:")
    for field_name, field in MainAgentSignature.output_fields.items():
        desc = getattr(field, 'json_schema_extra', {}).get('desc', 'No description') if hasattr(field, 'json_schema_extra') else 'No description'
        print(f"  - {field_name}: {desc}")

    print("\n" + "=" * 60)
    print("✅ Signature structure valid")
    print("\nNext: Use this signature in ClaudIO agent with dspy.ChainOfThought")
