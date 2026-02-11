"""
Tests for Data Expert module.

Tests DataExpert initialization, tool loading via MCPToolBridge,
and capabilities. Does not require LM Studio (no forward() tests).
"""

from unittest.mock import Mock

from clio_agent.experts.data_expert import DataExpert, MCPToolBridge
from clio_agent.tools.gateway import gateway


class TestMCPToolBridge:
    """Test the MCPToolBridge async-to-sync adapter."""

    def test_bridge_initialization(self):
        """Test bridge connects to gateway and discovers tools."""
        bridge = MCPToolBridge(gateway)
        try:
            tool_names = bridge.get_tool_names()
            assert len(tool_names) == 5
            assert "hdf5_analyze_file" in tool_names
            assert "hdf5_list_datasets" in tool_names
        finally:
            bridge.close()

    def test_bridge_call_tool(self, sample_hdf5):
        """Test bridge can call an MCP tool synchronously."""
        bridge = MCPToolBridge(gateway)
        try:
            result = bridge.call_tool("hdf5_analyze_file", {"filepath": sample_hdf5})
            assert "total_datasets" in result
            assert "error" not in result
        finally:
            bridge.close()

    def test_bridge_to_dspy_tools(self):
        """Test bridge converts MCP tools to DSPy Tool objects."""
        bridge = MCPToolBridge(gateway)
        try:
            tools = bridge.to_dspy_tools()
            assert len(tools) == 5
            tool_names = [t.name for t in tools]
            assert "hdf5_analyze_file" in tool_names
            assert "hdf5_list_datasets" in tool_names
            assert "hdf5_analyze_dataset" in tool_names
            assert "hdf5_check_compression" in tool_names
            assert "hdf5_optimize_chunking" in tool_names
        finally:
            bridge.close()

    def test_bridge_dspy_tool_callable(self, sample_hdf5):
        """Test DSPy tools from bridge are callable."""
        bridge = MCPToolBridge(gateway)
        try:
            tools = bridge.to_dspy_tools()
            analyze_tool = next(t for t in tools if t.name == "hdf5_analyze_file")
            result = analyze_tool(filepath=sample_hdf5)
            assert "total_datasets" in result
        finally:
            bridge.close()


class TestDataExpert:
    """Test Data Expert functionality."""

    def test_capabilities(self):
        """Test expert capabilities metadata."""
        caps = DataExpert.get_capabilities()

        assert caps["name"] == "Data Expert"
        assert "hdf5" in caps["keywords"]
        assert "parquet" in caps["keywords"]
        assert caps["priority"] == 1

    def test_expert_initialization(self):
        """Test expert can be initialized with real MCP tools."""
        expert = DataExpert()

        assert expert is not None
        assert hasattr(expert, "forward")
        assert hasattr(expert, "agent")
        assert hasattr(expert, "_tools")
        assert hasattr(expert, "_bridge")

    def test_expert_has_tools(self):
        """Test expert loads at least 4 HDF5 tools."""
        expert = DataExpert()
        assert len(expert._tools) >= 4

    def test_expert_has_react_agent(self):
        """Test expert uses ReAct, not ChainOfThought."""
        expert = DataExpert()
        # ReAct agent should have tools attribute
        assert hasattr(expert.agent, "tools")
        agent_type = type(expert.agent).__name__
        assert "ReAct" in agent_type

    def test_expert_tool_names(self):
        """Test expert has the expected HDF5 tools."""
        expert = DataExpert()
        tool_names = [t.name for t in expert._tools]
        assert "hdf5_analyze_file" in tool_names
        assert "hdf5_list_datasets" in tool_names

    def test_expert_with_arc_memory(self):
        """Test expert with ARC memory integration."""
        mock_arc = Mock()
        expert = DataExpert(arc_memory=mock_arc)

        assert expert is not None
        assert expert.arc_memory is mock_arc


class TestDataExpertSignature:
    """Test the DataExpertSignature prompt."""

    def test_signature_has_domain_prompt(self):
        """Test signature docstring is a substantial domain prompt (500+ words)."""
        from clio_agent.signatures.expert_sig import DataExpertSignature

        doc = DataExpertSignature.__doc__
        assert doc is not None
        word_count = len(doc.split())
        assert word_count >= 500, f"Signature prompt is only {word_count} words, need 500+"

    def test_signature_fields(self):
        """Test signature has the expected input/output fields."""
        from clio_agent.signatures.expert_sig import DataExpertSignature

        assert "question" in DataExpertSignature.input_fields
        assert "file_context" in DataExpertSignature.input_fields
        assert "analysis" in DataExpertSignature.output_fields
        assert "recommendations" in DataExpertSignature.output_fields
