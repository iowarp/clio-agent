#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Workflow Expert Module

Specializes in workflow automation and pipeline orchestration.

Key Capabilities:
- Workflow design and automation (Jarvis-CD integration)
- Pipeline orchestration strategies
- Task dependency management
- Error handling and retry logic
- Reproducibility and provenance

MCP Tools (to be implemented):
- jarvis_workflow_create: Create Jarvis-CD workflows
- jarvis_task_monitor: Monitor task execution
- pipeline_validator: Validate pipeline configurations
"""

import dspy
from typing import Dict, Any
import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/claudio/experts/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

from claudio.signatures.expert_sig import WorkflowExpertSignature


class WorkflowExpert(dspy.Module):
    """Workflow automation and pipeline orchestration expert."""

    def __init__(self, use_tools: bool = False):
        """Initialize Workflow Expert.

        Args:
            use_tools: If True, use ReAct with MCP tools (not yet implemented)
        """
        super().__init__()
        # TODO: Upgrade to ReAct when tools are ready
        self.generate = dspy.ChainOfThought(WorkflowExpertSignature)

    def forward(self, question: str, workflow_context: str = "") -> dspy.Prediction:
        """Generate workflow design and implementation guide.

        Args:
            question: User's question about workflows, automation
            workflow_context: Existing workflows, dependencies, goals

        Returns:
            dspy.Prediction with:
                - design: Workflow architecture
                - implementation: Implementation guide
        """
        return self.generate(question=question, workflow_context=workflow_context)

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for orchestrator routing."""
        return {
            "name": "Workflow Expert",
            "description": (
                "Specializes in workflow automation, pipeline orchestration, "
                "task scheduling, and reproducibility strategies"
            ),
            "keywords": [
                "workflow", "pipeline", "automation", "orchestration",
                "jarvis", "jarvis-cd", "task", "dependency", "dag",
                "scheduling", "retry", "error handling",
                "reproducibility", "provenance", "tracking",
                "airflow", "nextflow", "snakemake", "luigi",
                "batch processing", "job chain"
            ],
            "priority": 2,  # Medium priority
        }


if __name__ == "__main__":
    print("ClaudIO Workflow Expert Test")
    print("=" * 60)

    from claudio.config import setup_dspy

    try:
        lm = setup_dspy()
        expert = WorkflowExpert()

        test_question = "How do I automate my data processing pipeline?"
        test_context = "Daily: download data, run simulation, analyze results, generate plots"

        print(f"\nQuestion: {test_question}")
        print(f"Workflow Context: {test_context}")
        print("-" * 60)

        result = expert(question=test_question, workflow_context=test_context)

        print(f"\nWorkflow Design:\n{result.design[:300]}...")
        print(f"\nImplementation:\n{result.implementation[:300]}...")

        print("\n" + "=" * 60)
        print("✅ Workflow Expert working!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
