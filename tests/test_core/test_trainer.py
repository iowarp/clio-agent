"""Tests for training set generator and metric function.

Tests cover:
    - TrainingSetGenerator.generate() with sufficient data
    - TrainingSetGenerator.generate() with insufficient data (ValueError)
    - TrainingSetGenerator.get_available_counts()
    - clio_expert_metric with full output (score 1.0)
    - clio_expert_metric with empty output (score 0.0)
    - clio_expert_metric with partial output
    - clio_expert_metric in trace mode (returns bool)
    - clio_expert_metric with VisualizationExpert output fields
    - clio_expert_metric with error keywords
"""

import tempfile
import time

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Invocation
from clio_agent.optimizer.trainer import TrainingSetGenerator, clio_expert_metric


def _make_arc(tmp_path: str) -> ARCMemory:
    """Create a fresh ARCMemory instance in a temp directory."""
    return ARCMemory(data_dir=tmp_path, cache_capacity=100)


def _store_invocations(arc: ARCMemory, agent_id: str, count: int, status: str = "success"):
    """Store N invocations for a given agent with analysis+recommendations output."""
    for i in range(count):
        inv = Invocation(
            trace_id=f"trace-{agent_id}-{status}-{i}",
            session_id=f"session-{i % 5}",
            parent_trace_id=None,
            agent_id=agent_id,
            tier=2,
            source="native",
            started_at=time.time() + i,
            completed_at=time.time() + i + 0.5,
            duration_ms=500.0,
            status=status,
            input={
                "question": f"How to optimize data set {i}?",
                "file_context": f"File context for query {i}",
            },
            output={
                "analysis": f"Detailed analysis of dataset {i} with compression strategies and chunking recommendations.",
                "recommendations": f"Use gzip-6 compression for float64 arrays in dataset {i}. Consider rechunking.",
            },
        )
        arc.store_invocation(inv)


def _store_viz_invocations(arc: ARCMemory, count: int, status: str = "success"):
    """Store N visualization invocations."""
    for i in range(count):
        inv = Invocation(
            trace_id=f"trace-viz-{i}",
            session_id=f"session-{i % 5}",
            parent_trace_id=None,
            agent_id="visualization",
            tier=2,
            source="native",
            started_at=time.time() + i,
            completed_at=time.time() + i + 0.5,
            duration_ms=300.0,
            status=status,
            input={
                "question": f"Plot histogram of column {i}",
                "file_context": "",
            },
            output={
                "visualization_description": f"Histogram showing distribution of values in column {i} with 50 bins.",
                "file_path": f"/tmp/chart_{i}.png",
            },
        )
        arc.store_invocation(inv)


def test_generate_with_sufficient_data():
    """generate() returns correct dspy.Example list with 35 successful invocations."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        _store_invocations(arc, "data", 35)

        generator = TrainingSetGenerator(arc)
        examples = generator.generate("data", min_examples=30)

        assert len(examples) == 35
        assert isinstance(examples[0], dspy.Example)

        # Check input fields are set
        inputs = examples[0].inputs()
        assert "question" in inputs
        assert "file_context" in inputs

        # Check label fields exist
        assert hasattr(examples[0], "analysis")
        assert hasattr(examples[0], "recommendations")


def test_generate_with_insufficient_data():
    """generate() raises ValueError with <30 invocations and clear message."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        _store_invocations(arc, "data", 10)

        generator = TrainingSetGenerator(arc)

        with pytest.raises(ValueError) as exc_info:
            generator.generate("data", min_examples=30)

        error_msg = str(exc_info.value)
        assert "30" in error_msg
        assert "10" in error_msg
        assert "data" in error_msg


def test_generate_with_zero_invocations():
    """generate() raises ValueError when no invocations exist."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        generator = TrainingSetGenerator(arc)

        with pytest.raises(ValueError) as exc_info:
            generator.generate("data", min_examples=30)

        assert "0" in str(exc_info.value)


def test_generate_ignores_failures():
    """generate() only counts successful invocations."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        _store_invocations(arc, "data", 20, status="success")
        _store_invocations(arc, "data", 15, status="failure")

        generator = TrainingSetGenerator(arc)

        # Should fail: only 20 successes despite 35 total
        with pytest.raises(ValueError):
            generator.generate("data", min_examples=30)


def test_get_available_counts():
    """get_available_counts() returns correct per-agent counts."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        _store_invocations(arc, "data", 15)
        _store_invocations(arc, "analysis", 8)
        _store_invocations(arc, "data", 5, status="failure")

        generator = TrainingSetGenerator(arc)
        counts = generator.get_available_counts()

        assert counts["data"] == 15  # only successes
        assert counts["analysis"] == 8
        assert "failure" not in counts  # failures not counted


def test_metric_full_output_scores_1():
    """clio_expert_metric scores 1.0 for complete clean output."""
    example = dspy.Example(question="test", file_context="")
    pred = dspy.Prediction(
        analysis="This is a comprehensive analysis of data compression strategies for HDF5.",
        recommendations="Use gzip-6 for float64 arrays with rechunking to 1MB blocks.",
    )

    score = clio_expert_metric(example, pred)
    assert score == 1.0


def test_metric_empty_output_scores_0():
    """clio_expert_metric scores 0.0 for completely empty output."""
    example = dspy.Example(question="test", file_context="")
    pred = dspy.Prediction(analysis="", recommendations="")

    score = clio_expert_metric(example, pred)
    # Signal 1: 0 (empty analysis), Signal 2: 0 (empty rec), Signal 3: 0.3 (no error)
    assert score == pytest.approx(0.3)


def test_metric_no_fields_scores_03():
    """clio_expert_metric scores 0.3 for prediction with no expected fields."""
    example = dspy.Example(question="test", file_context="")
    pred = dspy.Prediction(answer="something else entirely")

    score = clio_expert_metric(example, pred)
    # No analysis, no recommendations, no errors -> only signal 3 fires
    assert score == pytest.approx(0.3)


def test_metric_partial_output():
    """clio_expert_metric scores partially for partial output."""
    example = dspy.Example(question="test", file_context="")

    # Only analysis, no recommendations -> 0.4 + 0.3 (no error) = 0.7
    pred = dspy.Prediction(
        analysis="This is a comprehensive analysis of the compression strategies.",
        recommendations="",
    )
    score = clio_expert_metric(example, pred)
    assert score == pytest.approx(0.7)


def test_metric_with_error_keywords():
    """clio_expert_metric deducts 0.3 for error keywords in output."""
    example = dspy.Example(question="test", file_context="")
    pred = dspy.Prediction(
        analysis="Error: failed to analyze the dataset due to corrupted headers.",
        recommendations="Unable to provide recommendations at this time.",
    )

    score = clio_expert_metric(example, pred)
    # Signal 1: 0.4 (long analysis), Signal 2: 0.3 (long rec), Signal 3: 0 (has errors)
    assert score == pytest.approx(0.7)


def test_metric_trace_mode_returns_bool():
    """clio_expert_metric returns boolean in trace (optimization) mode."""
    example = dspy.Example(question="test", file_context="")

    # Full output: score 1.0 >= 0.7 -> True
    pred_good = dspy.Prediction(
        analysis="Detailed analysis of compression strategies for large datasets.",
        recommendations="Apply gzip-6 compression and rechunk to 256KB blocks.",
    )
    result = clio_expert_metric(example, pred_good, trace="optimization")
    assert result is True
    assert isinstance(result, bool)

    # Empty output: score 0.3 < 0.7 -> False
    pred_bad = dspy.Prediction(analysis="", recommendations="")
    result = clio_expert_metric(example, pred_bad, trace="optimization")
    assert result is False


def test_metric_visualization_output():
    """clio_expert_metric handles VisualizationExpert output fields."""
    example = dspy.Example(question="test", file_context="")

    pred = dspy.Prediction(
        visualization_description="Histogram showing the distribution of temperature values across 1000 samples.",
        file_path="/tmp/charts/histogram_temp.png",
    )

    score = clio_expert_metric(example, pred)
    # Signal 1: 0.4 (viz desc > 20 chars), Signal 2: 0.3 (file_path exists)
    # Signal 3: 0.3 (no errors)
    assert score == pytest.approx(1.0)


def test_metric_visualization_no_file():
    """clio_expert_metric scores visualization without file_path."""
    example = dspy.Example(question="test", file_context="")
    pred = dspy.Prediction(
        visualization_description="A scatter plot showing correlation between pressure and temperature.",
        file_path="",
    )

    score = clio_expert_metric(example, pred)
    # Signal 1: 0.4 (desc > 20), Signal 2: 0 (no file, no recommendations), Signal 3: 0.3
    assert score == pytest.approx(0.7)
