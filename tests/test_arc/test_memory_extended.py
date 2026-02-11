"""Extended tests for ARC memory methods added in Phase 3.

Tests get_invocations_by_agent, variant record storage/retrieval,
and related query methods.
"""

import time

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Invocation, VariantRecord


@pytest.fixture
def arc(tmp_path):
    """Create an ARCMemory instance with tmp storage."""
    return ARCMemory(data_dir=str(tmp_path / "arc"))


def _make_invocation(trace_id, agent_id, status="success", duration_ms=100.0):
    """Helper to create an Invocation with minimal fields."""
    return Invocation(
        trace_id=trace_id,
        session_id="s1",
        parent_trace_id=None,
        agent_id=agent_id,
        tier=2,
        source="native",
        started_at=time.time(),
        completed_at=time.time(),
        duration_ms=duration_ms,
        status=status,
        input={"question": f"q-{trace_id}"},
        output={"analysis": f"a-{trace_id}"},
        tools_called=[],
        nanoagents_spawned=[],
        performance={"success": status == "success", "duration_ms": duration_ms},
        storage_tier="warm",
    )


class TestGetInvocationsByAgent:
    """Test get_invocations_by_agent query method."""

    def test_returns_matching_agent(self, arc):
        """Test returns invocations matching agent_id."""
        arc.store_invocation(_make_invocation("t1", "data"))
        arc.store_invocation(_make_invocation("t2", "analysis"))
        arc.store_invocation(_make_invocation("t3", "data"))

        result = arc.get_invocations_by_agent("data")
        assert len(result) == 2
        assert all(inv.agent_id == "data" for inv in result)

    def test_filters_by_status(self, arc):
        """Test status filter works."""
        arc.store_invocation(_make_invocation("t1", "data", status="success"))
        arc.store_invocation(_make_invocation("t2", "data", status="failure"))
        arc.store_invocation(_make_invocation("t3", "data", status="success"))

        result = arc.get_invocations_by_agent("data", status="success")
        assert len(result) == 2
        assert all(inv.status == "success" for inv in result)

    def test_respects_limit(self, arc):
        """Test limit parameter works."""
        for i in range(10):
            arc.store_invocation(_make_invocation(f"t{i}", "data"))

        result = arc.get_invocations_by_agent("data", limit=3)
        assert len(result) == 3

    def test_empty_for_unknown_agent(self, arc):
        """Test returns empty list for unknown agent_id."""
        arc.store_invocation(_make_invocation("t1", "data"))
        result = arc.get_invocations_by_agent("nonexistent")
        assert result == []

    def test_sorted_most_recent_first(self, arc):
        """Test results are sorted by started_at descending."""
        for i in range(3):
            inv = _make_invocation(f"t{i}", "data")
            inv.started_at = time.time() + i  # Later timestamps for higher i
            arc.store_invocation(inv)

        result = arc.get_invocations_by_agent("data")
        assert len(result) == 3
        # Most recent (highest started_at) should be first
        for i in range(len(result) - 1):
            assert result[i].started_at >= result[i + 1].started_at


class TestVariantRecordStorage:
    """Test store_variant_record and get_variant_records."""

    def test_store_and_retrieve_round_trip(self, arc):
        """Test storing and retrieving a variant record."""
        record = VariantRecord(
            variant_id="data_v1",
            agent_id="data",
            training_examples=50,
            before_score=0.6,
            after_score=0.85,
            improvement_delta=0.25,
            p_value=0.002,
            is_significant=True,
            is_active=True,
            file_path="variants/data_v1.json",
            dspy_version="3.1.3",
        )
        arc.store_variant_record(record)

        records = arc.get_variant_records("data")
        assert len(records) == 1
        assert records[0].variant_id == "data_v1"
        assert records[0].before_score == 0.6
        assert records[0].after_score == 0.85
        assert records[0].is_significant is True
        assert records[0].is_active is True

    def test_multiple_variants_for_agent(self, arc):
        """Test multiple variants for the same agent."""
        for i in range(3):
            record = VariantRecord(
                variant_id=f"data_v{i + 1}",
                agent_id="data",
                created_at=time.time() + i,
                before_score=0.5 + i * 0.1,
                after_score=0.7 + i * 0.1,
                improvement_delta=0.2,
                p_value=0.01,
                is_significant=True,
            )
            arc.store_variant_record(record)

        records = arc.get_variant_records("data")
        assert len(records) == 3

    def test_empty_for_unknown_agent(self, arc):
        """Test get_variant_records returns empty for unknown agent."""
        records = arc.get_variant_records("nonexistent")
        assert records == []

    def test_variants_sorted_by_created_at(self, arc):
        """Test variant records sorted by created_at descending."""
        for i in range(3):
            record = VariantRecord(
                variant_id=f"data_v{i + 1}",
                agent_id="data",
                created_at=time.time() + i,
            )
            arc.store_variant_record(record)

        records = arc.get_variant_records("data")
        for i in range(len(records) - 1):
            assert records[i].created_at >= records[i + 1].created_at

    def test_variant_update_replaces(self, arc):
        """Test storing a variant with same ID updates it."""
        record = VariantRecord(
            variant_id="data_v1",
            agent_id="data",
            is_active=False,
        )
        arc.store_variant_record(record)

        # Update active status
        record.is_active = True
        arc.store_variant_record(record)

        records = arc.get_variant_records("data")
        # Should have 1 record (updated in place on disk)
        assert len(records) == 1
        assert records[0].is_active is True

    def test_different_agents_isolated(self, arc):
        """Test variant records are isolated by agent_id."""
        arc.store_variant_record(VariantRecord(variant_id="data_v1", agent_id="data"))
        arc.store_variant_record(VariantRecord(variant_id="analysis_v1", agent_id="analysis"))

        data_records = arc.get_variant_records("data")
        analysis_records = arc.get_variant_records("analysis")

        assert len(data_records) == 1
        assert len(analysis_records) == 1
        assert data_records[0].variant_id == "data_v1"
        assert analysis_records[0].variant_id == "analysis_v1"
