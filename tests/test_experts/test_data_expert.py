"""
Tests for Data Expert module.

Tests DataExpert initialization, native tool execution via the MCP execution
boundary, and capabilities. Does not require LM Studio.
"""

from unittest.mock import Mock

import dspy
import pytest

from clio_agent.experts.data_expert import DataExpert, MCPToolBridge
from clio_agent.tools.execution import SyncMCPToolExecutor
from clio_agent.tools.gateway import gateway


class TestMCPToolBridge:
    """Test the MCPToolBridge async-to-sync adapter."""

    def test_bridge_initialization(self):
        """Test bridge connects to gateway and discovers tools."""
        bridge = MCPToolBridge(gateway)
        try:
            tool_names = bridge.get_tool_names()
            assert len(tool_names) >= 5  # At least 5 HDF5 tools (plus any others)
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
            assert len(tools) >= 5  # At least 5 HDF5 tools (plus any others)
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
        try:
            assert expert is not None
            assert hasattr(expert, "forward")
            assert hasattr(expert, "agent")
            assert hasattr(expert, "_tools")
            assert hasattr(expert, "_tool_executor")
            assert isinstance(expert._tool_executor, SyncMCPToolExecutor)
        finally:
            expert.close()

    def test_expert_has_tools(self):
        """Test expert loads at least 4 HDF5 tools."""
        expert = DataExpert()
        try:
            assert len(expert._tools) >= 4
        finally:
            expert.close()

    def test_expert_has_synthesis_module_not_react(self):
        """Test expert keeps DSPy only for synthesis, not tool execution."""
        expert = DataExpert()
        try:
            agent_type = type(expert.agent).__name__
            assert "Predict" in agent_type
            assert "ReAct" not in agent_type
        finally:
            expert.close()

    def test_expert_tool_names(self):
        """Test expert has the expected HDF5 tools."""
        expert = DataExpert()
        try:
            tool_names = [t.name for t in expert._tools]
            assert "hdf5_analyze_file" in tool_names
            assert "hdf5_list_datasets" in tool_names
        finally:
            expert.close()

    def test_expert_with_arc_memory(self):
        """Test expert with ARC memory integration."""
        mock_arc = Mock()
        expert = DataExpert(arc_memory=mock_arc)
        try:
            assert expert is not None
            assert expert.arc_memory is mock_arc
        finally:
            expert.close()

    def test_expert_accepts_tool_executor_boundary(self):
        """DataExpert should depend on a tool executor interface."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="hdf5_analyze_file",
                        desc="Fake HDF5 analyzer.",
                        args={},
                    )
                ]

            def close(self):
                self.closed = True

        executor = FakeExecutor()
        expert = DataExpert(tool_executor=executor)

        assert expert._tool_executor is executor
        expert.close()
        assert executor.closed is True

    def test_conceptual_synthesis_uses_dspy_result(self):
        """No-file conceptual questions should return provider synthesis when it succeeds."""
        expert = DataExpert()
        try:
            expert.agent = Mock(
                return_value=dspy.Prediction(
                    analysis="Use chunked HDF5 for partial reads.",
                    recommendations="Tune chunk payloads around the access pattern.",
                )
            )

            result = expert(question="How should I tune HDF5 chunks?")

            assert result.synthesis_source == "dspy"
            assert result.analysis == "Use chunked HDF5 for partial reads."
            assert "chunk payloads" in result.recommendations
        finally:
            expert.close()

    def test_conceptual_synthesis_failure_surfaces_error(self):
        """Provider-backed synthesis failures must not become canned guidance."""
        expert = DataExpert()
        try:
            expert.agent = Mock(side_effect=RuntimeError("provider unavailable"))

            with pytest.raises(RuntimeError, match="provider unavailable"):
                expert(question="How should I tune HDF5 chunks?")
        finally:
            expert.close()

    def test_empty_conceptual_synthesis_surfaces_error(self):
        """Empty provider output is a failure, not a fallback answer."""
        expert = DataExpert()
        try:
            expert.agent = Mock(return_value=dspy.Prediction(analysis="", recommendations=""))

            with pytest.raises(ValueError, match="empty analysis"):
                expert(question="How should I tune HDF5 chunks?")
        finally:
            expert.close()

    def test_expert_forward_uses_native_hdf5_tools(self, sample_hdf5):
        """Explicit HDF5 questions should run tools without LM calls."""
        expert = DataExpert()
        try:
            result = expert(question=f"What datasets are in {sample_hdf5}?")
            assert "simulation/temperature" in result.analysis
            assert result.synthesis_source == "deterministic"
            assert [tool.tool for tool in result.tool_provenance] == [
                "hdf5_analyze_file",
                "hdf5_list_datasets",
            ]
            arc_tool = result.tool_provenance[1].to_arc_tool_call()
            assert arc_tool.result["ok"] is True
            assert arc_tool.result["datasets"]["count"] == 3
        finally:
            expert.close()

    def test_expert_file_summary_includes_dataset_units(self, sample_hdf5):
        """File-level HDF5 summaries should expose dataset units when present."""
        expert = DataExpert()
        try:
            result = expert(question=f"What datasets and units are in {sample_hdf5}?")

            assert result.synthesis_source == "deterministic"
            assert "simulation/temperature" in result.analysis
            assert "units=Kelvin" in result.analysis
        finally:
            expert.close()

    def test_analyze_dataset_without_dataset_lists_available_datasets(self, sample_hdf5):
        """hdf5_analyze_dataset needs a dataset argument and should not fake one."""
        expert = DataExpert()
        try:
            result = expert(question=f"Run hdf5_analyze_dataset on {sample_hdf5}")

            assert result.synthesis_source == "deterministic"
            assert "needs a dataset path" in result.analysis
            assert "simulation/temperature" in result.analysis
            assert [tool.tool for tool in result.tool_provenance] == ["hdf5_list_datasets"]
        finally:
            expert.close()

    def test_analyze_dataset_with_dataset_uses_dataset_tool(self, sample_hdf5):
        """Named dataset analysis should call hdf5_analyze_dataset directly."""
        expert = DataExpert()
        try:
            result = expert(
                question=(f"Run hdf5_analyze_dataset on {sample_hdf5} for simulation/temperature")
            )

            assert result.synthesis_source == "deterministic"
            assert "Analyzed HDF5 dataset simulation/temperature" in result.analysis
            assert "statistics:" in result.analysis
            assert [tool.tool for tool in result.tool_provenance] == [
                "hdf5_list_datasets",
                "hdf5_analyze_dataset",
            ]
        finally:
            expert.close()

    def test_expert_rejects_invalid_hdf5_tool_shape(self):
        """Malformed HDF5 tool payloads should not produce file facts."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="hdf5_analyze_file",
                        desc="Fake HDF5 analyzer.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="hdf5_list_datasets",
                        desc="Fake HDF5 lister.",
                        args={},
                    ),
                ]

            def call_tool(self, name, args):
                assert name == "hdf5_analyze_file"
                return '{"filepath": "/tmp/broken.h5", "total_datasets": 99}'

            def close(self):
                self.closed = True

        expert = DataExpert(tool_executor=FakeExecutor())

        result = expert(question="Inspect /tmp/broken.h5")

        assert result.synthesis_source == "deterministic"
        assert result.analysis.startswith("Could not inspect HDF5 file")
        assert "99 datasets" not in result.analysis
        assert result.tool_provenance[0].ok is False
        error = result.tool_provenance[0].result["error"]
        assert error["type"] == "tool_contract"
        assert error["code"] == "invalid_result_shape"
        expert.close()


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
