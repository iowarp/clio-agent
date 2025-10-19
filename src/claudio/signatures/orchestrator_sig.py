#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Orchestrator Signature

Defines the input/output interface for the ClaudIO orchestrator.
The orchestrator routes user questions to the most appropriate expert
based on question analysis and expert capabilities.

This signature uses DSPy's declarative programming approach:
- Defines WHAT the orchestrator should do (routing)
- DSPy handles HOW through ChainOfThought reasoning
- No manual prompt engineering required
"""

import dspy
import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/claudio/signatures/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))


class OrchestratorSignature(dspy.Signature):
    """Route user questions to the most appropriate domain expert.

    The orchestrator analyzes the user's question and selects the best expert
    from: DataExpert, HPCExpert, AnalysisExpert, ResearchExpert, WorkflowExpert.

    Input:
        - question: User's question or request
        - available_experts: List of experts with their capabilities

    Output:
        - reasoning: Analysis of why a specific expert is best suited
        - selected_expert: ID of the selected expert (data, hpc, analysis, research, workflow)

    Example DSPy Usage:
        >>> router = dspy.ChainOfThought(OrchestratorSignature)
        >>> result = router(
        ...     question="How do I optimize HDF5 compression?",
        ...     available_experts="data: HDF5 expert\\nhpc: SLURM expert\\n..."
        ... )
        >>> print(result.selected_expert)  # "data"
        >>> print(result.reasoning)  # "Question asks about HDF5 optimization..."
    """

    # Input fields
    question: str = dspy.InputField(
        desc="User's question or request about scientific computing tasks"
    )
    available_experts: str = dspy.InputField(
        desc=(
            "List of available experts with their descriptions and keywords. "
            "Format: 'expert_id: description\\n  Keywords: keyword1, keyword2, ...'"
        )
    )

    # Output fields
    reasoning: str = dspy.OutputField(
        desc=(
            "Detailed analysis of the question content, identifying key topics, "
            "domains, and why a specific expert is the best match. "
            "Consider: domain keywords, task complexity, required tools."
        )
    )
    selected_expert: str = dspy.OutputField(
        desc=(
            "ID of the selected expert (must be one of: data, hpc, analysis, research, workflow). "
            "Return ONLY the expert ID, no additional text."
        )
    )


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("OrchestratorSignature Test")
    print("=" * 60)

    # This demonstrates the signature structure
    # In practice, this is used within dspy.ChainOfThought or dspy.ReAct

    print("\nSignature Fields:")
    print("-" * 60)

    print("\nInput Fields:")
    for field_name, field in OrchestratorSignature.input_fields.items():
        print(f"  - {field_name}: {field.json_schema_extra.get('desc', 'No description')}")

    print("\nOutput Fields:")
    for field_name, field in OrchestratorSignature.output_fields.items():
        print(f"  - {field_name}: {field.json_schema_extra.get('desc', 'No description')}")

    print("\n" + "=" * 60)
    print("✅ Signature structure valid")
    print("\nNext: Use this signature in ClaudIOOrchestrator with dspy.ChainOfThought")
