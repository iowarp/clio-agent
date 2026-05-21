"""
Tests for Analysis Expert module.

Tests AnalysisExpert initialization, native tool execution via the MCP execution
boundary, capabilities, and signature. Does not require LM Studio.
"""

import inspect
from unittest.mock import Mock

import dspy
import pytest

from clio_agent.experts.analysis_expert import AnalysisExpert
from clio_agent.tools.execution import SyncMCPToolExecutor


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
        try:
            for tool in expert._tools:
                assert tool.name.startswith("parquet_"), (
                    f"Tool {tool.name} does not have parquet_ prefix"
                )
        finally:
            expert.close()

    def test_analysis_expert_tool_count(self):
        """Test expert has exactly 3 parquet tools."""
        expert = AnalysisExpert()
        try:
            assert len(expert._tools) == 3
        finally:
            expert.close()

    def test_analysis_expert_tool_names(self):
        """Test expert has the expected parquet tools."""
        expert = AnalysisExpert()
        try:
            tool_names = [t.name for t in expert._tools]
            assert "parquet_analyze_schema" in tool_names
            assert "parquet_query_data" in tool_names
            assert "parquet_compute_statistics" in tool_names
        finally:
            expert.close()

    def test_analysis_expert_has_synthesis_module_not_react(self):
        """Test expert keeps DSPy only for synthesis, not tool execution."""
        expert = AnalysisExpert()
        try:
            agent_type = type(expert.agent).__name__
            assert "Predict" in agent_type
            assert "ReAct" not in agent_type
        finally:
            expert.close()

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
        try:
            sig = inspect.signature(expert.forward)
            params = list(sig.parameters.keys())
            assert "question" in params
            assert "file_context" in params
        finally:
            expert.close()

    def test_conceptual_synthesis_uses_dspy_result(self):
        """No-file conceptual questions should return provider synthesis when it succeeds."""
        expert = AnalysisExpert()
        try:
            expert.agent = Mock(
                return_value=dspy.Prediction(
                    analysis="Profile the columns needed by the question.",
                    recommendations="Inspect schema first, then compute targeted statistics.",
                )
            )

            result = expert(question="How should I profile this tabular dataset?")

            assert result.synthesis_source == "dspy"
            assert result.analysis == "Profile the columns needed by the question."
            assert "targeted statistics" in result.recommendations
        finally:
            expert.close()

    def test_conceptual_synthesis_failure_surfaces_error(self):
        """Provider-backed synthesis failures must not become canned guidance."""
        expert = AnalysisExpert()
        try:
            expert.agent = Mock(side_effect=RuntimeError("provider unavailable"))

            with pytest.raises(RuntimeError, match="provider unavailable"):
                expert(question="How should I profile this tabular dataset?")
        finally:
            expert.close()

    def test_empty_conceptual_synthesis_surfaces_error(self):
        """Empty provider output is a failure, not a fallback answer."""
        expert = AnalysisExpert()
        try:
            expert.agent = Mock(return_value=dspy.Prediction(analysis="", recommendations=""))

            with pytest.raises(ValueError, match="empty analysis"):
                expert(question="How should I profile this tabular dataset?")
        finally:
            expert.close()

    def test_analysis_expert_forward_uses_native_parquet_tools(self, sample_parquet):
        """Explicit Parquet questions should run tools without LM calls."""
        expert = AnalysisExpert()
        try:
            result = expert(question=f"Show statistics for temperature in {sample_parquet}")
            assert "Column statistics" in result.analysis
            assert "temperature" in result.analysis
            assert result.synthesis_source == "deterministic"
            assert [tool.tool for tool in result.tool_provenance] == [
                "parquet_analyze_schema",
                "parquet_compute_statistics",
            ]
            arc_tool = result.tool_provenance[0].to_arc_tool_call()
            assert arc_tool.result["ok"] is True
            assert arc_tool.result["columns"]["count"] == 3
        finally:
            expert.close()

    def test_parallel_file_validation_spawns_tool_backed_nanoagents(
        self, sample_hdf5, sample_parquet, tmp_path
    ):
        csv_path = tmp_path / "sensor_events.csv"
        csv_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        expert = AnalysisExpert()
        try:
            result = expert(
                question=(
                    f"Validate HDF5 structure for {sample_hdf5}, "
                    f"Parquet statistics for {sample_parquet}, and CSV schema for {csv_path}."
                )
            )
            spawns = result.nanoagents_spawned
            assert len(spawns) == 3
            tool_names = [row["name"] for spawn in spawns for row in spawn.get("tools_called", [])]
            assert any(name.startswith("hdf5_") for name in tool_names)
            assert any(name.startswith("parquet_") for name in tool_names)
            assert any(name.startswith("csv_") for name in tool_names)
            assert "Parallel validation completed" in result.analysis
            assert "data_validator" in result.analysis
            assert "analysis_validator" in result.analysis
            assert "csv_validator" in result.analysis
            provenance_names = [row.tool for row in result.tool_provenance]
            assert any(name.startswith("hdf5_") for name in provenance_names)
            assert any(name.startswith("parquet_") for name in provenance_names)
            assert any(name.startswith("csv_") for name in provenance_names)
        finally:
            expert.close()

    def test_analysis_expert_rejects_invalid_parquet_schema_shape(self):
        """Malformed Parquet schema payloads should not produce file facts."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="parquet_analyze_schema",
                        desc="Fake Parquet analyzer.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="parquet_compute_statistics",
                        desc="Fake Parquet stats.",
                        args={},
                    ),
                ]

            def call_tool(self, name, args):
                assert name == "parquet_analyze_schema"
                return '{"filepath": "/tmp/broken.parquet", "num_rows": 42, "columns": []}'

            def close(self):
                self.closed = True

        expert = AnalysisExpert(tool_executor=FakeExecutor())

        result = expert(question="Inspect /tmp/broken.parquet")

        assert result.synthesis_source == "deterministic"
        assert result.analysis.startswith("Could not inspect Parquet file")
        assert "42 rows" not in result.analysis
        assert result.tool_provenance[0].ok is False
        error = result.tool_provenance[0].result["error"]
        assert error["type"] == "tool_contract"
        assert error["code"] == "invalid_result_shape"
        expert.close()

    def test_analysis_expert_reports_stats_contract_failure_without_fake_values(self):
        """Optional stats failures should be explicit in the answer."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="parquet_analyze_schema",
                        desc="Fake Parquet analyzer.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="parquet_compute_statistics",
                        desc="Fake Parquet stats.",
                        args={},
                    ),
                ]

            def call_tool(self, name, args):
                if name == "parquet_analyze_schema":
                    return (
                        '{"filepath": "/tmp/test.parquet", "num_columns": 1, '
                        '"columns": [{"name": "temperature", "type": "double", '
                        '"nullable": true}], "num_rows": 10, "num_row_groups": 1, '
                        '"file_size_bytes": 128}'
                    )
                assert name == "parquet_compute_statistics"
                return '{"column": "temperature", "min": 1.0}'

            def close(self):
                self.closed = True

        expert = AnalysisExpert(tool_executor=FakeExecutor())

        result = expert(question="Show statistics for temperature in /tmp/test.parquet")

        assert "statistics unavailable" in result.analysis
        assert "min=1.0" not in result.analysis
        assert result.tool_provenance[-1].ok is False
        error = result.tool_provenance[-1].result["error"]
        assert error["type"] == "tool_contract"
        expert.close()

    def test_analysis_expert_uses_sync_tool_executor_boundary(self):
        """Default AnalysisExpert should use the explicit sync executor."""
        expert = AnalysisExpert()
        try:
            assert isinstance(expert._tool_executor, SyncMCPToolExecutor)
        finally:
            expert.close()

    def test_analysis_expert_with_arc_memory(self):
        """Test expert with ARC memory integration."""
        mock_arc = Mock()
        expert = AnalysisExpert(arc_memory=mock_arc)
        try:
            assert expert is not None
            assert expert.arc_memory is mock_arc
        finally:
            expert.close()

    def test_analysis_expert_accepts_tool_executor_boundary(self):
        """AnalysisExpert should filter tools from an injected executor."""

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
                        name="parquet_analyze_schema",
                        desc="Fake Parquet analyzer.",
                        args={},
                    ),
                ]

            def close(self):
                self.closed = True

        executor = FakeExecutor()
        expert = AnalysisExpert(tool_executor=executor)

        assert expert._tool_executor is executor
        assert [tool.name for tool in expert._tools] == ["parquet_analyze_schema"]
        expert.close()
        assert executor.closed is True

    def test_analysis_expert_initialization(self):
        """Test expert can be initialized and has required attributes."""
        expert = AnalysisExpert()
        try:
            assert expert is not None
            assert hasattr(expert, "forward")
            assert hasattr(expert, "agent")
            assert hasattr(expert, "_tools")
            assert hasattr(expert, "_tool_executor")
            assert hasattr(expert, "arc_memory")
        finally:
            expert.close()
