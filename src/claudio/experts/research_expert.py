#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Research Expert Module

Specializes in scientific literature search and research context.

Key Capabilities:
- Paper search and recommendations
- Citation analysis
- Research methodology guidance
- Domain knowledge synthesis
- Technical decision support with scientific backing

MCP Tools (to be implemented):
- arxiv_search: Search arXiv papers
- semantic_scholar_search: Semantic Scholar API
- citation_graph: Build citation networks
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

from claudio.signatures.expert_sig import ResearchExpertSignature


class ResearchExpert(dspy.Module):
    """Scientific literature and research context expert."""

    def __init__(self, use_tools: bool = False):
        """Initialize Research Expert.

        Args:
            use_tools: If True, use ReAct with MCP tools (not yet implemented)
        """
        super().__init__()
        # TODO: Upgrade to ReAct when tools are ready
        self.generate = dspy.ChainOfThought(ResearchExpertSignature)

    def forward(self, question: str, research_context: str = "") -> dspy.Prediction:
        """Generate research findings and methodology.

        Args:
            question: User's question about papers, research
            research_context: Domain, authors, timeframe

        Returns:
            dspy.Prediction with:
                - findings: Papers, citations, trends
                - methodology: Research methodology recommendations
        """
        return self.generate(question=question, research_context=research_context)

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for orchestrator routing."""
        return {
            "name": "Research Expert",
            "description": (
                "Specializes in scientific literature search, citation analysis, "
                "research methodology, and domain-specific knowledge synthesis"
            ),
            "keywords": [
                "paper", "research", "publication", "citation", "arxiv",
                "literature", "survey", "review", "methodology",
                "state of the art", "sota", "benchmark",
                "author", "conference", "journal", "doi",
                "algorithm", "technique", "approach", "method"
            ],
            "priority": 2,  # Medium priority
        }


if __name__ == "__main__":
    print("ClaudIO Research Expert Test")
    print("=" * 60)

    from claudio.config import setup_dspy

    try:
        lm = setup_dspy(use_lm_studio=True)
        expert = ResearchExpert()

        test_question = "Find recent papers on I/O optimization for exascale computing"
        test_context = "Interested in 2023-2024 publications, focus on parallel file systems"

        print(f"\nQuestion: {test_question}")
        print(f"Context: {test_context}")
        print("-" * 60)

        result = expert(question=test_question, context=test_context)
        print(f"Answer: {result.answer[:400]}...")

        print("\n✅ Research Expert working!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
