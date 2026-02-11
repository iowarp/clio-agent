"""
Tests for Analysis Expert module.

Tests AnalysisExpert initialization, tool loading via MCPToolBridge,
capabilities, and signature. Does not require LM Studio (no forward() tests
that call real LMs).
"""

import inspect
from unittest.mock import Mock

from clio_agent.experts.analysis_expert import AnalysisExpert
from clio_agent.experts.data_expert import MCPToolBridge


class TestAnalysisExpertSignature:
    """Test the AnalysisExpertSignature prompt."""

    def test_signature_has_domain_prompt(self):
        """Test signature docstring is a substantial domain prompt (500+ words)."""
        from clio_agent.signatures.analysis_sig import AnalysisExpertSignature

        doc = AnalysisExpertSignature.__doc__
        assert doc is not None
        word_count = len(doc.split())
        assert word_count >= 500, f"Signature prompt is only {word_count} words, need 500+"

    def test_signature_fields(self):
        """Test signature has the expected input/output fields."""
        from clio_agent.signatures.analysis_sig import AnalysisExpertSignature

        assert "question" in AnalysisExpertSignature.input_fields
        assert "file_context" in AnalysisExpertSignature.input_fields
        assert "analysis" in AnalysisExpertSignature.output_fields
        assert "recommendations" in AnalysisExpertSignature.output_fields


class TestAnalysisExpert:
    """Test Analysis Expert functionality."""

    def test_analysis_expert_loads_parquet_tools(self):
        """Test expert tools all start with parquet_ prefix."""
        expert = AnalysisExpert()
        for tool in expert._tools:
            assert tool.name.startswith("parquet_"), (
                f"Tool {tool.name} does not have parquet_ prefix"
            )

    def test_analysis_expert_tool_count(self):
        """Test expert has exactly 3 parquet tools."""
        expert = AnalysisExpert()
        assert len(expert._tools) == 3

    def test_analysis_expert_tool_names(self):
        """Test expert has the expected parquet tools."""
        expert = AnalysisExpert()
        tool_names = [t.name for t in expert._tools]
        assert "parquet_analyze_schema" in tool_names
        assert "parquet_query_data" in tool_names
        assert "parquet_compute_statistics" in tool_names

    def test_analysis_expert_has_react_agent(self):
        """Test expert uses ReAct, not ChainOfThought."""
        expert = AnalysisExpert()
        assert hasattr(expert.agent, "tools")
        agent_type = type(expert.agent).__name__
        assert "ReAct" in agent_type

    def test_analysis_expert_capabilities_keywords(self):
        """Test expert capabilities contain analysis-related keywords."""
        caps = AnalysisExpert.get_capabilities()
        keywords = caps["keywords"]
        assert "parquet" in keywords
        assert "statistics" in keywords
        assert "analysis" in keywords
        assert "schema" in keywords
        assert "distribution" in keywords
        assert "data quality" in keywords
        assert "columnar" in keywords

    def test_analysis_expert_capabilities_description(self):
        """Test expert capabilities have meaningful description."""
        caps = AnalysisExpert.get_capabilities()
        assert caps["name"] == "Analysis Expert"
        assert "statistical" in caps["description"].lower()
        assert "profiling" in caps["description"].lower()
        assert caps["priority"] == 2

    def test_analysis_expert_forward_signature(self):
        """Test forward method accepts question and file_context parameters."""
        expert = AnalysisExpert()
        sig = inspect.signature(expert.forward)
        params = list(sig.parameters.keys())
        assert "question" in params
        assert "file_context" in params

    def test_mcptoolbridge_reuse(self):
        """Test that AnalysisExpert imports MCPToolBridge, not redefines it."""
        # Verify AnalysisExpert uses the same MCPToolBridge class as DataExpert
        expert = AnalysisExpert()
        assert isinstance(expert._bridge, MCPToolBridge)

    def test_analysis_expert_with_arc_memory(self):
        """Test expert with ARC memory integration."""
        mock_arc = Mock()
        expert = AnalysisExpert(arc_memory=mock_arc)
        assert expert is not None
        assert expert.arc_memory is mock_arc

    def test_analysis_expert_initialization(self):
        """Test expert can be initialized and has required attributes."""
        expert = AnalysisExpert()
        assert expert is not None
        assert hasattr(expert, "forward")
        assert hasattr(expert, "agent")
        assert hasattr(expert, "_tools")
        assert hasattr(expert, "_bridge")
        assert hasattr(expert, "arc_memory")
