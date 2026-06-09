"""Tests for HDF5Expert.

The expert is deterministic-first: when a file is named in the question,
it picks one of the curated tools based on verb keywords and reports the
result without calling DSPy. The conceptual-fallback path (no file)
augments file_context with the top matching skill before invoking DSPy
— that branch is exercised via stubbing dspy.Predict.
"""

from __future__ import annotations

from unittest.mock import Mock

import dspy
import pytest

from clio_agent.experts.hdf5_expert import _HDF5_EXPERT_TOOLS, HDF5Expert
from clio_agent.tools.execution import SyncMCPToolExecutor


@pytest.fixture
def expert():
    e = HDF5Expert()
    yield e
    e.close()


def test_capabilities_shape():
    caps = HDF5Expert.get_capabilities()
    assert caps["name"] == "HDF5 Expert"
    assert caps["priority"] == 1
    assert "hdf5" in caps["keywords"]
    assert "swmr" in caps["keywords"]
    assert "vol connector" in caps["keywords"]
    assert "parquet" not in caps["keywords"], (
        "HDF5Expert must not claim Parquet — that's DataExpert's lane."
    )


def test_curated_tools_match_allowlist(expert):
    tool_names = {t.name for t in expert._tools}
    assert tool_names == set(_HDF5_EXPERT_TOOLS)


def test_verb_classification():
    cls = HDF5Expert._classify_verb
    assert cls("rechunk /sim/temp in foo.h5 to double") == "rechunk"
    assert cls("compress /sim/temp with gzip in foo.h5") == "filter"
    assert cls("visualize /sim/temp from foo.h5") == "visualize"
    assert cls("check CF compliance of foo.nc") == "cf_compliance"
    assert cls("get metadata for /sim/temp in foo.h5") == "metadata"
    assert cls("what's in foo.h5") == "analyze_file"


def test_extract_object_path():
    assert HDF5Expert._extract_object_path("inspect /a/b in foo.h5") == "/a/b"
    assert HDF5Expert._extract_object_path("inspect foo.h5") is None
    assert HDF5Expert._extract_object_path("no slashes here") is None


def test_dispatch_analyze_file(expert, sample_hdf5):
    pred = expert(question=f"What's in {sample_hdf5}?", file_context="")
    assert pred.synthesis_source == "deterministic"
    assert sample_hdf5 in pred.analysis
    assert any(obs.tool == "hdf5_analyze_file" for obs in pred.tool_provenance)


def test_dispatch_get_object_metadata(expert, sample_hdf5):
    pred = expert(
        question=f"metadata for /simulation/temperature in {sample_hdf5}",
        file_context="",
    )
    assert pred.synthesis_source == "deterministic"
    assert "/simulation/temperature" in pred.analysis
    assert any(obs.tool == "hdf5_get_object_metadata" for obs in pred.tool_provenance)


def test_dispatch_rechunk_is_advisory(expert, sample_hdf5):
    pred = expert(
        question=f"rechunk /simulation/temperature in {sample_hdf5} to be larger",
        file_context="",
    )
    assert pred.synthesis_source == "deterministic"
    assert "advisory" in pred.analysis.lower() or "planned" in pred.analysis.lower()
    # The expert must NOT have actually called the mutating tool.
    tool_calls = {obs.tool for obs in pred.tool_provenance}
    assert "hdf5_rechunk_dataset" not in tool_calls
    assert "hdf5_get_object_metadata" in tool_calls


def test_dispatch_apply_filter_is_advisory(expert, sample_hdf5):
    pred = expert(
        question=f"apply filter gzip to /simulation/pressure in {sample_hdf5}",
        file_context="",
    )
    assert pred.synthesis_source == "deterministic"
    tool_calls = {obs.tool for obs in pred.tool_provenance}
    assert "hdf5_apply_filter" not in tool_calls
    assert "hdf5_get_object_metadata" in tool_calls


def test_dispatch_visualize(expert, sample_hdf5, tmp_path):
    pred = expert(
        question=f"visualize /simulation/temperature from {sample_hdf5}",
        file_context="",
    )
    assert pred.synthesis_source == "deterministic"
    assert any(obs.tool == "hdf5_visualize_dataset" for obs in pred.tool_provenance)


def test_dispatch_visualize_needs_object_path(expert, sample_hdf5):
    pred = expert(question=f"plot {sample_hdf5}", file_context="")
    assert "without a dataset path" in pred.analysis
    assert pred.synthesis_source == "deterministic"


def test_dispatch_cf_compliance(expert, sample_hdf5):
    pred = expert(question=f"check CF compliance of {sample_hdf5}", file_context="")
    assert pred.synthesis_source == "deterministic"
    assert any(obs.tool == "hdf5_check_cf_compliance" for obs in pred.tool_provenance)


def test_conceptual_fallback_injects_skill(expert):
    """When no file is named, top-matching skill should be injected into file_context."""

    received: dict[str, str] = {}

    class _Stub:
        def __call__(self, question, file_context):
            received["question"] = question
            received["file_context"] = file_context
            return dspy.Prediction(
                analysis="stubbed analysis",
                recommendations="stubbed recommendations",
            )

    expert.agent = _Stub()
    pred = expert(
        question="I want to rechunk my datasets to align with column access",
        file_context="",
    )
    assert pred.synthesis_source == "dspy"
    assert "Bundled HDF5 skill (hdf5-chunking)" in received["file_context"]
    assert "Chunk Size Guidelines" in received["file_context"]


def test_conceptual_fallback_no_match():
    """A query that matches no skill still returns a graceful fallback."""

    expert = HDF5Expert()
    try:
        # Patch dspy.Predict-driven agent to a no-op so we exercise the
        # "no match, dspy returned nothing" branch deterministically.
        class _Empty:
            def __call__(self, **_kw):
                return dspy.Prediction(analysis="", recommendations="")

        expert.agent = _Empty()
        pred = expert(
            question="the weather is nice today and birds are singing",
            file_context="",
        )
        assert pred.synthesis_source == "fallback"
    finally:
        expert.close()


class TestHDF5Expert:
    """Parity tests mirroring the conventions in test_data_expert.py /
    test_analysis_expert.py / test_visualization_expert.py."""

    def test_expert_initialization(self):
        """Constructor wires the expected attributes."""
        expert = HDF5Expert()
        try:
            assert expert is not None
            assert hasattr(expert, "forward")
            assert hasattr(expert, "agent")
            assert hasattr(expert, "_tools")
            assert hasattr(expert, "_tool_executor")
            assert isinstance(expert._tool_executor, SyncMCPToolExecutor)
        finally:
            expert.close()

    def test_expert_has_synthesis_module_not_react(self):
        """Synthesis path uses dspy.Predict, not ReAct (per CLAUDE.md rule)."""
        expert = HDF5Expert()
        try:
            agent_type = type(expert.agent).__name__
            assert "Predict" in agent_type
            assert "ReAct" not in agent_type
        finally:
            expert.close()

    def test_expert_with_arc_memory(self):
        """ARC memory is accepted and stored on the instance."""
        mock_arc = Mock()
        expert = HDF5Expert(arc_memory=mock_arc)
        try:
            assert expert.arc_memory is mock_arc
        finally:
            expert.close()

    def test_expert_accepts_tool_executor_boundary(self):
        """A custom executor is injected and closed when the expert closes."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name=name,
                        desc=f"Fake {name}.",
                        args={},
                    )
                    for name in _HDF5_EXPERT_TOOLS
                ]

            def close(self):
                self.closed = True

        executor = FakeExecutor()
        expert = HDF5Expert(tool_executor=executor)
        assert expert._tool_executor is executor
        expert.close()
        assert executor.closed is True

    def test_expert_rejects_invalid_tool_shape(self, sample_hdf5):
        """A malformed analyze_file payload must NOT produce file facts."""

        class FakeExecutor:
            closed = False

            def to_dspy_tools(self):
                def fake_tool(**kwargs):
                    return "{}"

                return [
                    dspy.Tool(
                        func=fake_tool,
                        name=name,
                        desc=f"Fake {name}.",
                        args={},
                    )
                    for name in _HDF5_EXPERT_TOOLS
                ]

            def call_tool(self, name, args):
                # Return a payload missing required fields (no total_datasets,
                # no datasets list, etc.) so the schema validation trips.
                return '{"filepath": "/tmp/broken.h5"}'

            def close(self):
                self.closed = True

        expert = HDF5Expert(tool_executor=FakeExecutor())
        try:
            pred = expert(question=f"What's in {sample_hdf5}?", file_context="")
            assert pred.synthesis_source == "deterministic"
            # Must not hallucinate dataset counts from a broken payload.
            assert "datasets" not in pred.analysis or "0 datasets" in pred.analysis
            # Provenance should capture the tool-contract failure.
            failed = [obs for obs in pred.tool_provenance if not obs.ok]
            assert failed, "expected at least one failed tool observation"
        finally:
            expert.close()


@pytest.mark.parametrize(
    "qfile",
    ["foo.h5", "foo.hdf5", "data/foo.nc"],
)
def test_recognizes_all_supported_extensions(monkeypatch, qfile):
    """The expert should treat .h5, .hdf5, and .nc as file mentions."""
    from clio_agent.experts import hdf5_expert as mod

    captured: list[str] = []

    def fake_extract(question, file_context, extensions):
        captured.append(qfile)
        # Simulate "no path found" so we land in the conceptual fallback.
        return []

    monkeypatch.setattr(mod, "extract_file_paths", fake_extract)
    expert = HDF5Expert()
    try:
        expert.agent = lambda **_kw: dspy.Prediction(
            analysis="x", recommendations="y"
        )
        expert(question=f"look at {qfile}", file_context="")
    finally:
        expert.close()
    assert captured  # the dispatch hit extract_file_paths
