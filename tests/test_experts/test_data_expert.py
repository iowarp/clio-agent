"""
Tests for Data Expert module.

Tests DataExpert initialization, native tool execution via the MCP execution
boundary, and capabilities. Does not require LM Studio.
"""

from pathlib import Path
from unittest.mock import Mock

import dspy
import pytest

from clio_agent.experts.data_expert import DataExpert, MCPToolBridge
from clio_agent.experts.ndp_expert import NDPExpert
from clio_agent.tools.execution import SyncMCPToolExecutor
from clio_agent.tools.gateway import gateway


def _make_bp5_container(tmp_path: Path) -> Path:
    bp_path = tmp_path / "adios run" / "data.bp5"
    bp_path.mkdir(parents=True)
    (bp_path / "data.0").write_bytes(b"x" * 256)
    (bp_path / "md.0").write_bytes(b"m" * 64)
    (bp_path / "md.idx").write_bytes(b"i" * 16)
    (bp_path / "mmd.0").write_bytes(b"q" * 32)
    (bp_path / "profiling.json").write_text(
        '[{"rank":0,"transport_0":{"wbytes":256,"write":{"nCalls":4}}}]',
        encoding="utf-8",
    )
    return bp_path


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
            assert "adios_inspect_file" in tool_names
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
        assert "adios" in caps["keywords"]
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
        """Test top-level data expert owns only data-manager tools."""
        expert = DataExpert()
        try:
            tool_names = [t.name for t in expert._tools]
            assert "hdf5_analyze_file" in tool_names
            assert "hdf5_list_datasets" in tool_names
            assert "adios_inspect_file" in tool_names
            assert "ndp_list_organizations" not in tool_names
            assert "sac_inspect_archive" not in tool_names

            child_tool_names = [t.name for t in expert.ndp_expert._tools]
            assert "ndp_list_organizations" in child_tool_names
            assert "ndp_search_datasets" in child_tool_names
            assert "ndp_get_dataset_details" in child_tool_names
            assert "ndp_stage_resource" in child_tool_names
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

    def test_expert_forward_uses_native_adios_tools(self, tmp_path, monkeypatch):
        """Explicit ADIOS/BP questions should run BP tools without LM calls."""
        bp_path = _make_bp5_container(tmp_path)
        monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
        expert = DataExpert()
        try:
            result = expert(question=f'Inspect this ADIOS BP5 container: "{bp_path}"')

            assert result.synthesis_source == "deterministic"
            assert "Inspected ADIOS/BP5 container" in result.analysis
            assert "Profiling covers 1 ranks" in result.analysis
            assert [tool.tool for tool in result.tool_provenance] == ["adios_inspect_file"]
            assert result.metadata["format"] == "adios"
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

    def test_natural_dataset_focus_uses_dataset_tool(self, sample_hdf5):
        """Natural named-dataset questions should not require tool-shaped wording."""
        expert = DataExpert()
        try:
            result = expert(
                question=(
                    f"Focus on simulation/temperature inside {sample_hdf5}. "
                    "What shape, chunks, compression, and statistics matter for reads over time?"
                )
            )

            assert result.synthesis_source == "deterministic"
            assert "Analyzed HDF5 dataset simulation/temperature" in result.analysis
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

    def test_data_expert_uses_ndp_tools_for_catalog_discovery(self):
        """Natural NDP catalog requests should use data-owned discovery tools."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_list_organizations",
                        desc="List NDP organizations.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_search_datasets",
                        desc="Search NDP datasets.",
                        args={},
                    ),
                ]

            def call_tool(self, name, args):
                if name == "ndp_list_organizations":
                    assert args == {"name_filter": "noaa", "server": "global"}
                    return (
                        '{"organizations":["noaa-global-systems-laboratory"],'
                        '"count":1,"server":"global"}'
                    )
                assert name == "ndp_search_datasets"
                assert args == {
                    "server": "global",
                    "limit": 5,
                    "search_terms": ["climate"],
                }
                return (
                    '{"datasets":[{"id":"ds1","name":"climate-run",'
                    '"title":"Climate Run","owner_org":"noaa-global-systems-laboratory",'
                    '"resources":[{"format":"CSV"}]}],"count":1,"server":"global"}'
                )

            def close(self):
                self.closed = True

        expert = DataExpert(tool_executor=FakeExecutor())

        result = expert(
            question=(
                "Use the National Data Platform catalog to find NOAA climate datasets "
                "that could be useful for this analysis."
            )
        )

        assert result.synthesis_source == "deterministic"
        assert result.metadata["expert"] == "ndp_catalog"
        assert result.metadata["parent_expert"] == "data"
        assert "National Data Platform" in result.analysis
        assert "noaa-global-systems-laboratory" in result.analysis
        assert "Climate Run" in result.analysis
        assert [row.tool for row in result.tool_provenance] == [
            "ndp_list_organizations",
            "ndp_search_datasets",
        ]
        expert.close()

    def test_ndp_expert_owns_ndp_tools_directly(self):
        """The nested NDP expert should be executable without DataExpert internals."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_list_organizations",
                        desc="List NDP organizations.",
                        args={},
                    )
                ]

            def call_tool(self, name, args):
                assert name == "ndp_list_organizations"
                assert args == {"name_filter": "seism", "server": "global"}
                return '{"organizations":["earthscope"],"count":1,"server":"global"}'

            def close(self):
                self.closed = True

        expert = NDPExpert(tool_executor=FakeExecutor())
        result = expert(question="List NDP organizations for seismic data.")

        assert result.metadata["expert"] == "ndp_catalog"
        assert "earthscope" in result.analysis
        assert [row.tool for row in result.tool_provenance] == ["ndp_list_organizations"]

    def test_data_expert_searches_ndp_terms_independently(self):
        """Catalog discovery should fan out terms instead of over-constraining search."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_search_datasets",
                        desc="Search NDP datasets.",
                        args={},
                    )
                ]

            def call_tool(self, name, args):
                if name == "ndp_list_organizations":
                    assert args == {"name_filter": "seism", "server": "global"}
                    return '{"organizations":[],"count":0,"server":"global"}'
                assert name == "ndp_search_datasets"
                terms = args.get("search_terms")
                assert terms in (["seismic"], ["seismological"], ["waveform"])
                if terms == ["seismic"]:
                    return (
                        '{"datasets":[{"id":"salton","name":"salton-sea-seismic-data",'
                        '"title":"Salton Sea Seismic Data","owner_org":"ucr",'
                        '"notes":"Seismic waveform data in MiniSEED format.",'
                        '"resource_names":["Salton Sea Seismic Waveforms"],'
                        '"resource_formats":["MiniSEED"],'
                        '"resources":[{"format":"MiniSEED"}]}],"count":1,"server":"global"}'
                    )
                if terms == ["seismological"]:
                    return (
                        '{"datasets":[{"id":"ridgecrest","name":"ridgecrest-lidar",'
                        '"title":"Ridgecrest Earthquake Lidar","owner_org":"opentopography",'
                        '"resources":[{"format":"TIFF"}]}],"count":1,"server":"global"}'
                    )
                return (
                    '{"datasets":[{"id":"ridgecrest","name":"ridgecrest-lidar",'
                    '"title":"Ridgecrest Earthquake Lidar","owner_org":"opentopography",'
                    '"resources":[{"format":"TIFF"}]}],"count":1,"server":"global"}'
                )

            def close(self):
                self.closed = True

        expert = DataExpert(tool_executor=FakeExecutor())

        result = expert(
            question=(
                "Find seismic data from a seismological organization on NDP and "
                "inspect usable resources."
            )
        )

        assert result.synthesis_source == "deterministic"
        assert "Salton Sea Seismic Data" in result.analysis
        assert "MiniSEED" in result.analysis
        assert "three-axis plotting remain blocked" in result.analysis
        assert "staged SAC/MiniSEED/waveform file" in result.recommendations
        assert "Ridgecrest Earthquake Lidar" in result.analysis
        search_calls = [
            row.params["search_terms"]
            for row in result.tool_provenance
            if row.tool == "ndp_search_datasets"
        ]
        assert search_calls == [
            ["seismic"],
            ["seismological"],
            ["waveform"],
        ]
        expert.close()

    def test_data_expert_surfaces_ndp_staging_blocker_to_parent(self):
        """NDP child failures should return evidence, not fetch unrelated SAC data."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_search_datasets",
                        desc="Search NDP datasets.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_get_dataset_details",
                        desc="Get NDP details.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_stage_resource",
                        desc="Stage NDP resources.",
                        args={},
                    ),
                ]

            def call_tool(self, name, args):
                if name == "ndp_list_organizations":
                    return '{"organizations":[],"count":0,"server":"global"}'
                if name == "ndp_search_datasets":
                    return (
                        '{"datasets":[{"id":"salton","name":"salton-sea-seismic-data",'
                        '"title":"Salton Sea Seismic Data","owner_org":"ucr",'
                        '"notes":"Seismic waveform data in MiniSEED format.",'
                        '"resource_names":["Salton Sea Seismic Waveforms"]}],'
                        '"count":1,"server":"global"}'
                    )
                if name == "ndp_get_dataset_details":
                    assert args["dataset_identifier"] == "salton"
                    return (
                        '{"id":"salton","name":"salton-sea-seismic-data",'
                        '"title":"Salton Sea Seismic Data","resource_count":1,'
                        '"resource_urls":["osdf:///ndp/public/ucr_seis/Data_Salton"]}'
                    )
                if name == "ndp_stage_resource":
                    assert args["dataset_identifier"] == "salton"
                    return (
                        '{"error":{"type":"tool_error",'
                        '"code":"unsupported_resource_transport",'
                        '"message":"OSDF transport is not staged directly.",'
                        '"next_action":"Use Pelican to stage this resource.",'
                        '"details":{"transport":"osdf"}}}'
                    )
                raise AssertionError(f"unexpected child recovery tool call: {name}")

            def close(self):
                self.closed = True

        expert = DataExpert(tool_executor=FakeExecutor())

        result = expert(
            question=(
                "Find seismic data from a seismological organization on NDP. Pick a "
                "usable dataset, inspect the data, analyze the signal across three axes, "
                "and produce a plot."
            )
        )

        assert result.synthesis_source == "deterministic"
        assert "Staging note" in result.analysis
        assert "unsupported_resource_transport" in result.analysis
        assert "Parent recovery should decide" in result.analysis
        assert "EarthScope SAC waveform" not in result.analysis
        assert "/tmp/earthscope.sac" not in result.analysis
        assert result.metadata["staging"]["status"] == "blocked"
        assert result.metadata["staging"]["reason"] == "staging_failed"
        assert result.metadata["staging"]["attempts"][0]["error"]["code"] == (
            "unsupported_resource_transport"
        )
        assert "delegate_to_utility_download" in result.metadata["staging"][
            "recommended_parent_actions"
        ]
        assert [row.tool for row in result.tool_provenance] == [
            "ndp_list_organizations",
            "ndp_search_datasets",
            "ndp_search_datasets",
            "ndp_search_datasets",
            "ndp_get_dataset_details",
            "ndp_stage_resource",
        ]
        expert.close()

    def test_ndp_staging_recovers_to_alternative_candidate(self):
        """NDP staging should keep trying bounded alternatives after one resource fails."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_search_datasets",
                        desc="Search NDP datasets.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_get_dataset_details",
                        desc="Get NDP details.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_stage_resource",
                        desc="Stage NDP resources.",
                        args={},
                    ),
                ]

            def call_tool(self, name, args):
                if name == "ndp_list_organizations":
                    return '{"organizations":[],"count":0,"server":"global"}'
                if name == "ndp_search_datasets":
                    return (
                        '{"datasets":['
                        '{"id":"bad","name":"bad-waveform","title":"Bad Waveform",'
                        '"notes":"SAC waveform data","resource_count":1,'
                        '"resource_urls":["https://down.example/bad.tar"]},'
                        '{"id":"good","name":"good-waveform","title":"Good Waveform",'
                        '"notes":"SAC waveform data","resource_count":1,'
                        '"resource_urls":["https://up.example/good.tar"]}'
                        '],"count":2,"server":"global"}'
                    )
                if name == "ndp_get_dataset_details":
                    return (
                        '{"id":"%s","resources":[{"name":"waveforms.tar",'
                        '"url":"https://example.test/waveforms.tar"}]}'
                    ) % args["dataset_identifier"]
                assert name == "ndp_stage_resource"
                if args["dataset_identifier"] == "bad":
                    return (
                        '{"error":{"type":"tool_error","code":"resource_download_failed",'
                        '"message":"download timed out","next_action":"try another mirror"}}'
                    )
                return (
                    '{"staged":true,"path":"/tmp/good-waveforms.tar",'
                    '"size_bytes":2048,"url":"https://up.example/good.tar"}'
                )

            def close(self):
                self.closed = True

        expert = NDPExpert(tool_executor=FakeExecutor())

        result = expert(
            question=(
                "Find a seismic waveform dataset in NDP, stage it, inspect the data, "
                "analyze the signal, and plot it."
            )
        )

        assert "CLIO staged the selected NDP resource at /tmp/good-waveforms.tar" in result.analysis
        assert "after 1 failed attempt(s)" in result.analysis
        stage_calls = [row for row in result.tool_provenance if row.tool == "ndp_stage_resource"]
        assert [row.params["dataset_identifier"] for row in stage_calls] == ["bad", "good"]
        expert.close()

    def test_ndp_waveform_staging_does_not_fall_back_to_lidar_geojson(self):
        """Waveform requests should not recover to unrelated datasets or SAC fallbacks."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_search_datasets",
                        desc="Search NDP datasets.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_get_dataset_details",
                        desc="Get NDP details.",
                        args={},
                    ),
                    dspy.Tool(
                        func=fake_tool,
                        name="ndp_stage_resource",
                        desc="Stage NDP resources.",
                        args={},
                    ),
                ]

            def call_tool(self, name, args):
                if name == "ndp_list_organizations":
                    return '{"organizations":[],"count":0,"server":"global"}'
                if name == "ndp_search_datasets":
                    return (
                        '{"datasets":['
                        '{"id":"wave","name":"waveform-data","title":"ScP Waveforms",'
                        '"notes":"SAC waveform data","resource_count":1,'
                        '"resource_urls":["https://hive.example/wave.tar"]},'
                        '{"id":"lidar","name":"central-seismic-lidar",'
                        '"title":"Central Seismic Zone Lidar",'
                        '"notes":"Lidar point cloud spatial extents",'
                        '"resource_count":1,"resource_formats":["GEOJSON"],'
                        '"resource_urls":["https://example.test/spatial_extents.geojson"]}'
                        '],"count":2,"server":"global"}'
                    )
                if name == "ndp_get_dataset_details":
                    assert args["dataset_identifier"] == "wave"
                    return (
                        '{"id":"wave","resources":[{"name":"wave.tar",'
                        '"url":"https://hive.example/wave.tar"}]}'
                    )
                if name == "ndp_stage_resource":
                    assert args["dataset_identifier"] == "wave"
                    return (
                        '{"error":{"type":"tool_error","code":"webget_failed",'
                        '"message":"curl failed","next_action":"try another waveform mirror"}}'
                    )
                raise AssertionError(f"unexpected child recovery tool call: {name}")

            def close(self):
                self.closed = True

        expert = NDPExpert(tool_executor=FakeExecutor())

        result = expert(
            question=(
                "Find a bounded seismic waveform dataset, stage it, inspect waveform "
                "content, compute trace statistics, and produce a plot."
            )
        )

        assert "none could be staged by the NDP Catalog Expert" in result.analysis
        assert "Parent recovery should decide" in result.analysis
        assert "EarthScope SAC waveform" not in result.analysis
        assert result.metadata["staging"]["status"] == "blocked"
        assert result.metadata["staging"]["attempts"][0]["dataset_identifier"] == "wave"
        assert result.metadata["staging"]["attempts"][0]["error"]["code"] == "webget_failed"
        stage_calls = [row for row in result.tool_provenance if row.tool == "ndp_stage_resource"]
        detail_calls = [
            row for row in result.tool_provenance if row.tool == "ndp_get_dataset_details"
        ]
        fallback_calls = [
            row for row in result.tool_provenance if row.tool == "sac_fetch_earthscope_waveform"
        ]
        assert [row.params["dataset_identifier"] for row in detail_calls] == ["wave"]
        assert [row.params["dataset_identifier"] for row in stage_calls] == ["wave"]
        assert fallback_calls == []
        assert stage_calls[0].result["error"]["handled"] is True
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
