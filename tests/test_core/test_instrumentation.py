"""Tests for optimizer instrumentation decorator and MetricsAggregator.

Tests cover:
    - instrumented_forward decorator logging on success and failure
    - _extract_output from dspy.Prediction-like objects
    - MetricsAggregator.compute_expert_metrics
    - VariantRecord schema encode/decode round-trip
    - get_invocations_by_agent filtering
    - store_variant_record + get_variant_records
"""

import tempfile
import time

import dspy

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import (
    Invocation,
    VariantRecord,
    decode_variant_record,
    encode_variant_record,
)
from clio_agent.optimizer.instrumentation import (
    MetricsAggregator,
    _extract_output,
    instrumented_forward,
)


def _make_arc(tmp_path: str) -> ARCMemory:
    """Create a fresh ARCMemory instance in a temp directory."""
    return ARCMemory(data_dir=tmp_path, cache_capacity=100)


class FakeExpert:
    """Fake expert for testing instrumented_forward."""

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        return dspy.Prediction(
            analysis="This is a detailed analysis of HDF5 compression strategies.",
            recommendations="Use gzip-6 for float64 data arrays.",
        )


class FailingExpert:
    """Expert that always raises."""

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        raise RuntimeError("Expert crashed on purpose")


def test_instrumented_forward_logs_success():
    """Decorator logs Invocation with status=success on successful call."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        expert = FakeExpert()

        wrapped = instrumented_forward(arc, "data")(expert.forward)
        result = wrapped(question="How to optimize HDF5?")

        assert result.analysis is not None
        assert "gzip" in result.recommendations.lower()

        # Verify invocation was stored
        invocations = arc.get_invocations_by_agent("data")
        assert len(invocations) == 1
        inv = invocations[0]
        assert inv.agent_id == "data"
        assert inv.status == "success"
        assert inv.tier == 2
        assert inv.duration_ms > 0
        assert "question" in inv.input
        assert "analysis" in inv.output


def test_instrumented_forward_logs_failure():
    """Decorator logs Invocation with status=failure on exception."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        expert = FailingExpert()

        wrapped = instrumented_forward(arc, "analysis")(expert.forward)

        try:
            wrapped(question="Analyze this data")
        except RuntimeError:
            pass  # expected

        invocations = arc.get_invocations_by_agent("analysis")
        assert len(invocations) == 1
        inv = invocations[0]
        assert inv.status == "failure"
        assert "error" in inv.output
        assert "crashed" in inv.output["error"].lower()


def test_instrumented_forward_preserves_exception():
    """Decorator re-raises the original exception."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        expert = FailingExpert()

        wrapped = instrumented_forward(arc, "data")(expert.forward)

        raised = False
        try:
            wrapped(question="test")
        except RuntimeError as e:
            raised = True
            assert "crashed on purpose" in str(e)

        assert raised


def test_extract_output_from_prediction():
    """_extract_output extracts string fields from dspy.Prediction."""
    pred = dspy.Prediction(
        analysis="Detailed analysis here",
        recommendations="Use gzip compression",
    )
    output = _extract_output(pred)
    assert "analysis" in output
    assert "recommendations" in output
    assert output["analysis"] == "Detailed analysis here"


def test_extract_output_truncates():
    """_extract_output truncates values to 500 chars."""
    long_text = "x" * 1000
    pred = dspy.Prediction(analysis=long_text)
    output = _extract_output(pred)
    assert len(output["analysis"]) == 500


def test_metrics_aggregator_success_rate():
    """MetricsAggregator computes correct success_rate and avg_latency."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)

        # Store 3 success + 1 failure for "data"
        for i, status in enumerate(["success", "success", "success", "failure"]):
            inv = Invocation(
                trace_id=f"trace-{i}",
                session_id="session-1",
                parent_trace_id=None,
                agent_id="data",
                tier=2,
                source="native",
                started_at=time.time(),
                completed_at=time.time(),
                duration_ms=100.0 * (i + 1),  # 100, 200, 300, 400
                status=status,
                input={"question": f"q{i}"},
                output={},
            )
            arc.store_invocation(inv)

        aggregator = MetricsAggregator(arc)
        metrics = aggregator.compute_expert_metrics("data")

        assert metrics["total_invocations"] == 4
        assert metrics["success_rate"] == 0.75
        assert metrics["avg_latency_ms"] == 250.0  # (100+200+300+400)/4


def test_metrics_aggregator_empty():
    """MetricsAggregator returns zeros for unknown agent."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)
        aggregator = MetricsAggregator(arc)
        metrics = aggregator.compute_expert_metrics("unknown")

        assert metrics["total_invocations"] == 0
        assert metrics["success_rate"] == 0.0
        assert metrics["avg_latency_ms"] == 0.0


def test_variant_record_roundtrip():
    """VariantRecord encodes and decodes through msgpack correctly."""
    record = VariantRecord(
        variant_id="data_v2",
        agent_id="data",
        created_at=1700000000.0,
        training_examples=50,
        before_score=0.65,
        after_score=0.82,
        improvement_delta=0.17,
        p_value=0.003,
        is_significant=True,
        is_active=True,
        file_path="variants/data_v2.json",
        dspy_version="3.1.3",
        metadata={"optimizer": "SIMBA"},
    )

    encoded = encode_variant_record(record)
    decoded = decode_variant_record(encoded)

    assert decoded.variant_id == "data_v2"
    assert decoded.agent_id == "data"
    assert decoded.training_examples == 50
    assert decoded.before_score == 0.65
    assert decoded.after_score == 0.82
    assert decoded.improvement_delta == 0.17
    assert decoded.p_value == 0.003
    assert decoded.is_significant is True
    assert decoded.is_active is True
    assert decoded.file_path == "variants/data_v2.json"
    assert decoded.dspy_version == "3.1.3"
    assert decoded.metadata == {"optimizer": "SIMBA"}


def test_variant_record_defaults():
    """VariantRecord has correct defaults for optional fields."""
    record = VariantRecord(variant_id="test_v1", agent_id="test")

    assert record.training_examples == 0
    assert record.before_score == 0.0
    assert record.p_value == 1.0
    assert record.is_significant is False
    assert record.is_active is False
    assert record.file_path == ""
    assert record.dspy_version == ""
    assert record.metadata == {}
    assert record.created_at > 0


def test_get_invocations_by_agent_filtered():
    """get_invocations_by_agent returns filtered results by agent_id and status."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)

        # Store invocations for two agents
        for i, (agent, status) in enumerate(
            [
                ("data", "success"),
                ("data", "failure"),
                ("analysis", "success"),
                ("data", "success"),
            ]
        ):
            inv = Invocation(
                trace_id=f"trace-{i}",
                session_id="session-1",
                parent_trace_id=None,
                agent_id=agent,
                tier=2,
                source="native",
                started_at=time.time() + i,
                completed_at=time.time() + i,
                duration_ms=100.0,
                status=status,
                input={"question": f"q{i}"},
                output={},
            )
            arc.store_invocation(inv)

        # All data invocations
        data_invs = arc.get_invocations_by_agent("data")
        assert len(data_invs) == 3

        # Only successful data invocations
        data_success = arc.get_invocations_by_agent("data", status="success")
        assert len(data_success) == 2

        # Analysis invocations
        analysis_invs = arc.get_invocations_by_agent("analysis")
        assert len(analysis_invs) == 1


def test_store_and_get_variant_records():
    """store_variant_record and get_variant_records work correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        arc = _make_arc(tmp)

        r1 = VariantRecord(
            variant_id="data_v1",
            agent_id="data",
            created_at=1000.0,
            before_score=0.5,
            after_score=0.7,
        )
        r2 = VariantRecord(
            variant_id="data_v2",
            agent_id="data",
            created_at=2000.0,
            before_score=0.7,
            after_score=0.85,
        )
        r3 = VariantRecord(
            variant_id="analysis_v1",
            agent_id="analysis",
            created_at=1500.0,
        )

        arc.store_variant_record(r1)
        arc.store_variant_record(r2)
        arc.store_variant_record(r3)

        data_records = arc.get_variant_records("data")
        assert len(data_records) == 2
        assert data_records[0].variant_id == "data_v2"  # most recent first
        assert data_records[1].variant_id == "data_v1"

        analysis_records = arc.get_variant_records("analysis")
        assert len(analysis_records) == 1
        assert analysis_records[0].variant_id == "analysis_v1"


def test_extract_output_capture_failure_logs_reason(caplog):
    """A prediction whose fields cannot be read warns instead of vanishing (#772)."""
    import logging

    class ExplodingPrediction:
        def keys(self):
            raise RuntimeError("fields exploded")

    with caplog.at_level(logging.WARNING, logger="clio_agent.optimizer.instrumentation"):
        output = _extract_output(ExplodingPrediction())

    assert output == {}
    matching = [
        r for r in caplog.records if "reason=prediction_output_capture_failed" in r.getMessage()
    ]
    assert matching, "expected a structured prediction_output_capture_failed warning"
