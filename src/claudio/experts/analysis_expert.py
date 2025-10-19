#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Analysis Expert Module

Specializes in data analysis, visualization, and ML workflows.

Key Capabilities:
- Statistical analysis strategies
- Visualization design and recommendations
- ML/AI workflow architecture
- Data preprocessing pipelines
- Analysis tool selection

MCP Tools (to be implemented):
- plot_generation: Generate visualization code
- statistical_analysis: Perform statistical tests
- ml_pipeline_generator: Create ML workflow templates
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

from claudio.signatures.expert_sig import AnalysisExpertSignature


class AnalysisExpert(dspy.Module):
    """Data analysis and visualization expert."""

    def __init__(self, use_tools: bool = False):
        """Initialize Analysis Expert.

        Args:
            use_tools: If True, use ReAct with MCP tools (not yet implemented)
        """
        super().__init__()
        # TODO: Upgrade to ReAct when tools are ready
        self.generate = dspy.ChainOfThought(AnalysisExpertSignature)

    def forward(self, question: str, data_context: str = "") -> dspy.Prediction:
        """Generate analysis approach and code examples.

        Args:
            question: User's question about analysis, viz, stats
            data_context: Data types, size, variables, goals

        Returns:
            dspy.Prediction with:
                - approach: Analysis strategy
                - code_example: Python code for the analysis
        """
        return self.generate(question=question, data_context=data_context)

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for orchestrator routing."""
        return {
            "name": "Analysis Expert",
            "description": (
                "Specializes in data analysis, statistical methods, visualization design, "
                "and machine learning workflow recommendations"
            ),
            "keywords": [
                "analysis", "visualization", "plot", "chart", "graph",
                "statistics", "stats", "correlation", "regression",
                "machine learning", "ml", "deep learning", "ai",
                "numpy", "pandas", "matplotlib", "seaborn", "plotly",
                "scikit-learn", "pytorch", "tensorflow",
                "preprocessing", "feature engineering", "model"
            ],
            "priority": 1,
        }


if __name__ == "__main__":
    print("ClaudIO Analysis Expert Test")
    print("=" * 60)

    from claudio.config import setup_dspy

    try:
        lm = setup_dspy(use_lm_studio=True)
        expert = AnalysisExpert()

        test_question = "How should I visualize my simulation time-series data?"
        test_context = "1 million timesteps, 100 variables, looking for patterns and anomalies"

        print(f"\nQuestion: {test_question}")
        print(f"Context: {test_context}")
        print("-" * 60)

        result = expert(question=test_question, context=test_context)
        print(f"Answer: {result.answer[:400]}...")

        print("\n✅ Analysis Expert working!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
