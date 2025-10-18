#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
#   "pydantic>=2.6.0",
# ]
# ///

"""
Warpio DSPy POC - Expert Modules

Simple expert modules demonstrating DSPy patterns without MCP integration.
Run with: uv run experts.py
"""

import dspy
from typing import Dict, Any


# ============================================================================
# SIGNATURES
# ============================================================================

class GeneralAssistantSignature(dspy.Signature):
    """General purpose assistant for answering questions."""

    question: str = dspy.InputField(desc="User's question")
    context: str = dspy.InputField(desc="Conversation context", default="")

    answer: str = dspy.OutputField(desc="Helpful answer to the question")


class CodeHelperSignature(dspy.Signature):
    """Programming and code assistance expert."""

    question: str = dspy.InputField(desc="Programming question or task")
    language: str = dspy.InputField(desc="Programming language", default="python")

    explanation: str = dspy.OutputField(desc="Explanation of the solution")
    code: str = dspy.OutputField(desc="Code example or solution")


class DataAnalystSignature(dspy.Signature):
    """Data analysis and scientific computing expert."""

    question: str = dspy.InputField(desc="Data analysis question")
    data_context: str = dspy.InputField(desc="Information about the data", default="")

    analysis: str = dspy.OutputField(desc="Analysis and insights")
    recommendations: str = dspy.OutputField(desc="Recommendations or next steps")


# ============================================================================
# EXPERT MODULES
# ============================================================================

class GeneralAssistant(dspy.Module):
    """General purpose assistant expert."""

    def __init__(self):
        super().__init__()
        self.generate = dspy.ChainOfThought(GeneralAssistantSignature)

    def forward(self, question: str, context: str = "") -> dspy.Prediction:
        """Generate helpful answer to general questions."""
        return self.generate(question=question, context=context)

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for orchestrator."""
        return {
            "name": "General Assistant",
            "description": "Answers general questions, provides explanations, and helps with various topics",
            "keywords": ["general", "help", "explain", "what", "why", "how"],
            "priority": 3  # Lower priority (fallback expert)
        }


class CodeHelper(dspy.Module):
    """Programming and code assistance expert."""

    def __init__(self):
        super().__init__()
        self.generate = dspy.ChainOfThought(CodeHelperSignature)

    def forward(self, question: str, language: str = "python") -> dspy.Prediction:
        """Generate code solutions and explanations."""
        return self.generate(question=question, language=language)

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for orchestrator."""
        return {
            "name": "Code Helper",
            "description": "Provides programming assistance, code examples, debugging help, and best practices",
            "keywords": ["code", "programming", "function", "bug", "debug", "script", "python", "javascript", "c++"],
            "priority": 2  # Medium priority
        }


class DataAnalyst(dspy.Module):
    """Data analysis and scientific computing expert."""

    def __init__(self):
        super().__init__()
        self.generate = dspy.ChainOfThought(DataAnalystSignature)

    def forward(self, question: str, data_context: str = "") -> dspy.Prediction:
        """Generate data analysis and recommendations."""
        return self.generate(question=question, data_context=data_context)

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for orchestrator."""
        return {
            "name": "Data Analyst",
            "description": "Analyzes data, provides statistical insights, and recommends data processing approaches",
            "keywords": ["data", "analysis", "statistics", "visualization", "dataset", "csv", "pandas", "numpy"],
            "priority": 2  # Medium priority
        }


# ============================================================================
# EXPERT REGISTRY
# ============================================================================

def get_all_experts() -> Dict[str, dspy.Module]:
    """Get all available expert instances."""
    return {
        "general": GeneralAssistant(),
        "code": CodeHelper(),
        "data": DataAnalyst()
    }


def get_expert_capabilities() -> Dict[str, Dict[str, Any]]:
    """Get capabilities of all experts."""
    return {
        "general": GeneralAssistant.get_capabilities(),
        "code": CodeHelper.get_capabilities(),
        "data": DataAnalyst.get_capabilities()
    }


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("Warpio DSPy POC - Expert Modules Test")
    print("=" * 50)

    # Import config
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from config import setup_dspy

    try:
        print("\nInitializing with LM Studio...")
        lm = setup_dspy(use_lm_studio=True)

        # Test each expert
        experts = get_all_experts()

        print("\n1. Testing General Assistant:")
        result = experts["general"](question="What is Python?", context="")
        print(f"Answer: {result.answer[:200]}...")

        print("\n2. Testing Code Helper:")
        result = experts["code"](question="How do I read a CSV file?", language="python")
        print(f"Explanation: {result.explanation[:200]}...")
        print(f"Code: {result.code[:200]}...")

        print("\n3. Testing Data Analyst:")
        result = experts["data"](question="How should I clean missing data?", data_context="CSV with 1000 rows")
        print(f"Analysis: {result.analysis[:200]}...")
        print(f"Recommendations: {result.recommendations[:200]}...")

        print("\n✅ All experts working!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("Make sure LM Studio is running at http://127.0.0.1:1234")
