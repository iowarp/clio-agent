"""
Tests for Analysis Expert module.

Tests AnalysisExpert initialization, native tool execution via the MCP execution
boundary, capabilities, and signature. Does not require LM Studio.
"""

import inspect
from unittest.mock import Mock

import dspy
import pytest

from clio_agent.experts.analysis_expert import AnalysisExpert, _detect_parallel_items
from clio_agent.experts.sac_format_expert import SACFormatExpert
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

    def test_analysis_expert_loads_analysis_tools(self):
        """Test expert tools are analysis-owned gateway tools."""
        expert = AnalysisExpert()
        try:
            for tool in expert._tools:
                assert tool.name.startswith("parquet_"), (
                    f"Tool {tool.name} does not have an analysis-owned prefix"
                )
        finally:
            expert.close()

    def test_analysis_expert_tool_count(self):
        """Test top-level analysis owns Parquet gateway tools."""
        expert = AnalysisExpert()
        try:
            assert len(expert._tools) == 3
        finally:
            expert.close()

    def test_analysis_expert_tool_names(self):
        """Test expert has the expected Parquet tools."""
        expert = AnalysisExpert()
        try:
            tool_names = [t.name for t in expert._tools]
            assert "parquet_analyze_schema" in tool_names
            assert "parquet_query_data" in tool_names
            assert "parquet_compute_statistics" in tool_names
            assert "sac_compute_trace_statistics" not in tool_names
            assert "ndp_list_organizations" not in tool_names
            assert "ndp_search_datasets" not in tool_names
            assert "ndp_get_dataset_details" not in tool_names

            child_tool_names = [t.name for t in expert.sac_format_expert._tools]
            assert "sac_compute_trace_statistics" in child_tool_names
        finally:
            expert.close()

    def test_sac_format_expert_owns_sac_tools(self):
        """SAC-specific tools should live on the nested format expert."""
        expert = SACFormatExpert()
        try:
            tool_names = [t.name for t in expert._tools]
            assert "sac_inspect_archive" in tool_names
            assert "sac_compute_trace_statistics" in tool_names
            assert "sac_plot_traces" in tool_names
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
        assert "ndp" not in keywords
        assert "dataset discovery" not in keywords

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

    def test_none_conceptual_synthesis_surfaces_error(self):
        """Literal None provider text is a failure, not a normal answer."""
        expert = AnalysisExpert()
        try:
            expert.agent = Mock(
                return_value=dspy.Prediction(analysis="None", recommendations="None")
            )

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

    def test_natural_multi_file_prompt_spawns_tool_backed_nanoagents(
        self, sample_hdf5, sample_parquet, tmp_path
    ):
        """Users should not need to say nanoagent or tool for obvious fan-out work."""
        csv_path = tmp_path / "sensor_events.csv"
        csv_path.write_text("event_id,status\n1,ok\n", encoding="utf-8")
        expert = AnalysisExpert()
        try:
            result = expert(
                question=(
                    "I have three related files from the same experiment: "
                    f"{sample_hdf5}, {sample_parquet}, and {csv_path}. "
                    "Give me a cross-file triage summary and tell me whether they line up."
                )
            )
            spawns = result.nanoagents_spawned
            assert len(spawns) == 3
            tool_names = [row["name"] for spawn in spawns for row in spawn.get("tools_called", [])]
            assert any(name.startswith("hdf5_") for name in tool_names)
            assert any(name.startswith("parquet_") for name in tool_names)
            assert any(name.startswith("csv_") for name in tool_names)
            assert "Parallel validation completed" in result.analysis
        finally:
            expert.close()

    def test_detect_parallel_items_for_natural_multi_file_prompt(self):
        prompt = (
            "I have three related files from the same experiment: "
            "C:\\data\\run.h5, C:\\data\\measurements.parquet, and C:\\data\\events.csv. "
            "Give me a cross-file triage summary and tell me whether they line up."
        )

        items = _detect_parallel_items(prompt)

        assert len(items) == 3
        assert any(item.endswith("run.h5") for item in items)
        assert any(item.endswith("measurements.parquet") for item in items)
        assert any(item.endswith("events.csv") for item in items)

    def test_single_file_profile_does_not_spawn_from_commas(self, sample_parquet):
        expert = AnalysisExpert()
        try:
            result = expert(
                question=(
                    f"Profile the facility measurements in this Parquet file: {sample_parquet}. "
                    "I care about the schema, row groups, and whether temperature, pressure, "
                    "humidity, and anomaly_score look sane."
                )
            )

            assert not hasattr(result, "nanoagents_spawned")
            assert "Parallel validation completed" not in result.analysis
        finally:
            expert.close()

    def test_retained_multi_source_synthesis_does_not_narrow_to_first_file(
        self, sample_hdf5, sample_parquet, tmp_path
    ):
        """Compacted multi-source evidence should not become first-file tool inspection."""
        csv_path = tmp_path / "sensor_events.csv"
        csv_path.write_text("event_id,status,operator_note\n1,ok,checked\n", encoding="utf-8")
        expert = AnalysisExpert()
        try:
            expert.agent = Mock(
                return_value=dspy.Prediction(
                    analysis=(
                        "Retained evidence cites /plasma/electron_temperature, "
                        "anomaly_score, event_id, and BP5 profiling caveats."
                    ),
                    recommendations="Reinspect CSV semantics and BP5 variables before review.",
                )
            )

            result = expert(
                question=(
                    "After compaction, use the retained evidence to decide whether all stages "
                    "are ready for collaborator review."
                ),
                file_context=(
                    "[Retained session context]\n[compact summary]\n"
                    f"HDF5: {sample_hdf5} includes /plasma/electron_temperature.\n"
                    f"Parquet: {sample_parquet} includes anomaly_score.\n"
                    f"CSV: {csv_path} includes event_id and operator_note.\n"
                    "BP5: C:\\data\\run.bp5 profiling succeeded but variables need ADIOS2."
                    "\n\n[exact retained evidence index]\n"
                    "Paths:\n"
                    f"- {sample_hdf5}\n"
                    f"- {sample_parquet}\n"
                    f"- {csv_path}\n"
                    "- C:\\data\\run.bp5\n"
                    "Identifiers:\n"
                    "- /plasma/electron_temperature\n"
                    "- anomaly_score\n"
                    "- operator_note\n"
                    "Caveats/errors:\n"
                    "- BP5 variables need ADIOS2."
                ),
            )

            assert result.synthesis_source == "dspy"
            assert not result.tool_provenance
            assert expert.agent.call_count == 1
            assert "anomaly_score" in result.analysis
            assert "BP5" in result.analysis
            assert "Retained evidence anchors" in result.analysis
            assert str(csv_path) in result.analysis
        finally:
            expert.close()

    def test_single_parquet_triage_ignores_unrelated_retained_file_context(
        self, sample_hdf5, sample_parquet
    ):
        """Old retained context should not turn a single-file follow-up into synthesis."""
        expert = AnalysisExpert()
        try:
            expert.agent = Mock(
                return_value=dspy.Prediction(
                    analysis="should not synthesize",
                    recommendations="should not synthesize",
                )
            )

            result = expert(
                question=(
                    "Based on the Parquet file we just profiled, compute whatever schema "
                    "or column statistics you need for a quick anomaly triage view."
                ),
                file_context=(
                    "[Retained session context]\n"
                    f"Earlier HDF5 note mentioned {sample_hdf5}.\n"
                    f"Current session file: {sample_parquet}"
                ),
            )

            assert result.synthesis_source == "deterministic"
            assert result.metadata["format"] == "parquet"
            assert not expert.agent.called
            tools = [tool.tool for tool in result.tool_provenance]
            assert tools[0] == "parquet_analyze_schema"
            assert "parquet_compute_statistics" in tools
        finally:
            expert.close()

    def test_explicit_csv_path_overrides_retained_multi_file_context(
        self, sample_hdf5, sample_parquet, tmp_path
    ):
        """Retained multi-file context should not steal an explicit CSV request."""
        csv_path = tmp_path / "sensor_events.csv"
        csv_path.write_text(
            "event_id,status,operator_note,timestamp_utc\n1,ok,checked,2026-05-24T00:00:00Z\n",
            encoding="utf-8",
        )
        expert = AnalysisExpert()
        try:
            expert.agent = Mock(
                return_value=dspy.Prediction(
                    analysis="should not synthesize",
                    recommendations="should not synthesize",
                )
            )
            result = expert(
                question=(
                    f"This event stream came with the run: {csv_path}. "
                    "What columns does it contain, and where are status and operator_note?"
                ),
                file_context=(
                    "[Retained session context]\n"
                    f"Earlier HDF5 note mentioned {sample_hdf5}.\n"
                    f"Earlier Parquet note mentioned {sample_parquet}.\n"
                    "Earlier BP5 note mentioned D:\\runs\\gray_scott.bp5."
                ),
            )

            assert result.metadata["format"] == "csv"
            assert not expert.agent.called
            assert [tool.tool for tool in result.tool_provenance] == ["csv_read_table"]
            assert "event_id" in result.analysis
            assert "operator_note" in result.analysis
        finally:
            expert.close()

    def test_anomaly_triage_selects_semantic_numeric_columns(self):
        columns = [
            {"name": "sample_id", "type": "int64"},
            {"name": "run_id", "type": "string"},
            {"name": "site", "type": "string"},
            {"name": "temperature_k", "type": "double"},
            {"name": "pressure_pa", "type": "double"},
            {"name": "humidity_pct", "type": "double"},
            {"name": "vibration_mm_s", "type": "double"},
            {"name": "anomaly_score", "type": "double"},
        ]

        selected = AnalysisExpert._select_stat_columns(
            "Compute a quick anomaly triage view.",
            columns,
        )

        assert selected == [
            "anomaly_score",
            "temperature_k",
            "pressure_pa",
            "humidity_pct",
        ]

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
