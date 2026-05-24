"""Additional ARC Memory tests for coverage of uncovered paths.

Covers: _maybe_evict_index, get_invocation (cache miss path), get_session_invocations,
store_metrics, get_metrics (with/without period, cache miss), store_context, get_context
(cache miss), cache_tool_result, get_cached_tool_result, store_dataset_profile,
get_dataset_profile (disk fallback), get_session_profiles, store_procedural_memory,
get_procedural_memories, query_metrics_by_time_range, get_lsm_stats, clear_cache,
clear_all, _parse_timestamp (string timestamps).
"""

import time

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import (
    Context,
    Conversation,
    DatasetProfile,
    Invocation,
    InvocationStats,
    LatencyStats,
    Message,
    Metrics,
    ProceduralMemory,
    UserSatisfactionStats,
)


@pytest.fixture
def arc(tmp_path):
    return ARCMemory(data_dir=str(tmp_path / "arc"))


def _inv(trace_id, session_id="s1", agent_id="data"):
    """Create minimal Invocation."""
    now = time.time()
    return Invocation(
        trace_id=trace_id,
        session_id=session_id,
        parent_trace_id=None,
        agent_id=agent_id,
        tier=2,
        source="native",
        started_at=now,
        completed_at=now,
        duration_ms=100.0,
        status="success",
        input={"q": trace_id},
        output={"a": trace_id},
        tools_called=[],
        nanoagents_spawned=[],
        performance={},
    )


class TestMaybeEvictIndex:
    """Test _maybe_evict_index."""

    def test_no_eviction_under_limit(self, arc):
        """Index below limit should not evict."""
        arc._index_max_entries = 100
        for i in range(50):
            arc._conv_index.insert((f"s{i}", float(i)), {"session_id": f"s{i}"})
        arc._maybe_evict_index(arc._conv_index)
        assert len(arc._conv_index) == 50

    def test_eviction_over_limit(self, arc):
        """Index over limit should evict oldest entries."""
        arc._index_max_entries = 10
        for i in range(20):
            arc._conv_index.insert((f"s{i}", float(i)), {"session_id": f"s{i}"})
        arc._maybe_evict_index(arc._conv_index)
        # Should have ~90% of max = 9
        assert len(arc._conv_index) <= 10


class TestGetInvocation:
    """Test get_invocation cache miss path."""

    def test_cache_hit(self, arc):
        """get_invocation should find from cache."""
        inv = _inv("trace-1")
        arc.store_invocation(inv)
        result = arc.get_invocation("trace-1")
        assert result is not None
        assert result.trace_id == "trace-1"

    def test_disk_fallback(self, arc):
        """get_invocation should load from disk when not in cache."""
        inv = _inv("trace-2")
        arc.store_invocation(inv)
        arc.clear_cache()  # Force cache miss
        result = arc.get_invocation("trace-2")
        assert result is not None
        assert result.trace_id == "trace-2"

    def test_missing_returns_none(self, arc):
        """get_invocation should return None for missing trace."""
        result = arc.get_invocation("nonexistent")
        assert result is None


class TestGetSessionInvocations:
    """Test get_session_invocations."""

    def test_returns_session_invocations(self, arc):
        """Should return invocations for the given session."""
        arc.store_invocation(_inv("t1", session_id="s1"))
        arc.store_invocation(_inv("t2", session_id="s1"))
        arc.store_invocation(_inv("t3", session_id="s2"))

        result = arc.get_session_invocations("s1")
        assert len(result) >= 1

    def test_empty_session(self, arc):
        """Should return empty list for session with no invocations."""
        result = arc.get_session_invocations("empty")
        assert result == []


class TestStoreGetMetrics:
    """Test store_metrics and get_metrics."""

    def _make_metrics(self, agent_id="data", period="2025-01"):
        return Metrics(
            agent_id=agent_id,
            tier=2,
            period=period,
            computed_at=time.time(),
            invocations=InvocationStats(100, 95, 5, 0, 0.95),
            latency=LatencyStats(1500.0, 1200.0, 2500.0, 4000.0, 200.0, 8000.0),
            user_satisfaction=UserSatisfactionStats(50, 45, 5, 0.90),
        )

    def test_store_and_get_with_period(self, arc):
        """Store and retrieve metrics by period."""
        m = self._make_metrics()
        arc.store_metrics(m)
        result = arc.get_metrics("data", period="2025-01")
        assert result is not None
        assert result.agent_id == "data"
        assert result.period == "2025-01"

    def test_get_latest_without_period(self, arc):
        """Get latest metrics when no period specified."""
        arc.store_metrics(self._make_metrics(period="2025-01"))
        arc.store_metrics(self._make_metrics(period="2025-02"))
        result = arc.get_metrics("data")
        assert result is not None
        assert result.period == "2025-02"

    def test_get_missing_returns_none(self, arc):
        """Should return None for nonexistent metrics."""
        assert arc.get_metrics("nonexistent", period="2025-01") is None

    def test_get_latest_no_files(self, arc):
        """Should return None when no metrics files exist."""
        assert arc.get_metrics("data") is None

    def test_disk_fallback(self, arc):
        """Should load from disk on cache miss."""
        arc.store_metrics(self._make_metrics())
        arc.clear_cache()
        result = arc.get_metrics("data", period="2025-01")
        assert result is not None


class TestStoreGetContext:
    """Test store_context and get_context."""

    def test_store_and_get(self, arc):
        """Store and retrieve context."""
        now = time.time()
        ctx = Context(domain="hdf5", created_at=now, updated_at=now)
        arc.store_context(ctx)
        result = arc.get_context("hdf5")
        assert result is not None
        assert result.domain == "hdf5"

    def test_disk_fallback(self, arc):
        """Should load from disk on cache miss."""
        now = time.time()
        ctx = Context(domain="test", created_at=now, updated_at=now)
        arc.store_context(ctx)
        arc.clear_cache()
        result = arc.get_context("test")
        assert result is not None

    def test_missing_returns_none(self, arc):
        """Should return None for nonexistent context."""
        assert arc.get_context("nonexistent") is None


class TestToolCache:
    """Test cache_tool_result and get_cached_tool_result."""

    def test_cache_and_retrieve(self, arc):
        """Should cache and retrieve tool result."""
        arc.cache_tool_result("hdf5", "analyze", {"path": "a.h5"}, {"size": 100})
        result = arc.get_cached_tool_result("hdf5", "analyze", {"path": "a.h5"})
        assert result == {"size": 100}

    def test_cache_miss(self, arc):
        """Should return None for uncached tool result."""
        result = arc.get_cached_tool_result("hdf5", "analyze", {"path": "missing.h5"})
        assert result is None

    def test_different_args_different_keys(self, arc):
        """Different arguments should produce different cache entries."""
        arc.cache_tool_result("hdf5", "analyze", {"path": "a.h5"}, "result_a")
        arc.cache_tool_result("hdf5", "analyze", {"path": "b.h5"}, "result_b")
        assert arc.get_cached_tool_result("hdf5", "analyze", {"path": "a.h5"}) == "result_a"
        assert arc.get_cached_tool_result("hdf5", "analyze", {"path": "b.h5"}) == "result_b"


class TestDatasetProfiles:
    """Test store_dataset_profile and get_dataset_profile."""

    def _make_profile(self, session_id="s1", filepath="/data/test.h5"):
        return DatasetProfile(
            session_id=session_id,
            filepath=filepath,
            file_format="hdf5",
            created_by="data",
            created_at=time.time(),
        )

    def test_store_and_get(self, arc):
        """Store and retrieve profile."""
        p = self._make_profile()
        arc.store_dataset_profile(p)
        result = arc.get_dataset_profile("s1", "/data/test.h5")
        assert result is not None
        assert result.filepath == "/data/test.h5"

    def test_disk_fallback(self, arc):
        """Should load from disk on cache miss."""
        p = self._make_profile()
        arc.store_dataset_profile(p)
        arc.clear_cache()
        result = arc.get_dataset_profile("s1", "/data/test.h5")
        assert result is not None

    def test_missing_returns_none(self, arc):
        """Should return None for nonexistent profile."""
        assert arc.get_dataset_profile("s1", "/missing.h5") is None

    def test_get_session_profiles(self, arc):
        """Should return all profiles for a session."""
        arc.store_dataset_profile(self._make_profile("s1", "/a.h5"))
        arc.store_dataset_profile(self._make_profile("s1", "/b.h5"))
        arc.store_dataset_profile(self._make_profile("s2", "/c.h5"))

        profiles = arc.get_session_profiles("s1")
        assert len(profiles) == 2

    def test_get_session_profiles_empty(self, arc):
        """Should return empty list for session with no profiles."""
        assert arc.get_session_profiles("empty") == []

    def test_session_profiles_deduplication(self, arc):
        """Profiles from cache and disk should not duplicate."""
        p = self._make_profile("s1", "/a.h5")
        arc.store_dataset_profile(p)
        # Profile is in both cache and disk -- should only appear once
        profiles = arc.get_session_profiles("s1")
        filepaths = [pr.filepath for pr in profiles]
        assert filepaths.count("/a.h5") == 1


class TestProceduralMemory:
    """Test store_procedural_memory and get_procedural_memories."""

    def _make_mem(self, session_id="s1", expert_id="data", idx=0):
        return ProceduralMemory(
            session_id=session_id,
            expert_id=expert_id,
            pattern_type="success",
            description=f"pattern-{idx}",
            context={"file_type": "hdf5"},
            outcome="good",
            learned_at=time.time() + idx,
        )

    def test_store_and_get(self, arc):
        """Store and retrieve procedural memory."""
        m = self._make_mem()
        arc.store_procedural_memory(m)
        result = arc.get_procedural_memories("s1")
        assert len(result) >= 1

    def test_filter_by_expert(self, arc):
        """Should filter by expert_id."""
        arc.store_procedural_memory(self._make_mem(expert_id="data", idx=0))
        arc.store_procedural_memory(self._make_mem(expert_id="analysis", idx=1))

        result = arc.get_procedural_memories("s1", expert_id="data")
        assert all(m.expert_id == "data" for m in result)

    def test_sorted_most_recent_first(self, arc):
        """Should return most recent first."""
        for i in range(3):
            arc.store_procedural_memory(self._make_mem(idx=i))

        result = arc.get_procedural_memories("s1")
        for i in range(len(result) - 1):
            assert result[i].learned_at >= result[i + 1].learned_at

    def test_respects_limit(self, arc):
        """Should respect limit parameter."""
        for i in range(10):
            arc.store_procedural_memory(self._make_mem(idx=i))

        result = arc.get_procedural_memories("s1", limit=3)
        assert len(result) <= 3

    def test_disk_fallback(self, arc):
        """Should load from disk on cache miss."""
        arc.store_procedural_memory(self._make_mem())
        arc.clear_cache()
        result = arc.get_procedural_memories("s1")
        assert len(result) >= 1


class TestQueryMetrics:
    """Test query_metrics_by_time_range and get_lsm_stats."""

    def test_range_query(self, arc):
        """Should query LSM for time range."""
        now = time.time()
        arc.store_invocation(_inv("t1"))
        result = arc.query_metrics_by_time_range(now - 10, now + 10)
        assert isinstance(result, list)

    def test_lsm_stats(self, arc):
        """Should return LSM statistics."""
        stats = arc.get_lsm_stats()
        assert isinstance(stats, dict)
        assert "write_count" in stats


class TestClearOperations:
    """Test clear_cache and clear_all."""

    def test_clear_cache(self, arc):
        """clear_cache should empty cache but keep disk."""
        arc.store_invocation(_inv("t1"))
        arc.clear_cache()
        stats = arc.get_cache_stats()
        assert stats["size"] == 0
        # Disk should still have the data
        result = arc.get_invocation("t1")
        assert result is not None

    def test_clear_all(self, arc):
        """clear_all should remove everything."""
        now = time.time()
        arc.store_invocation(_inv("t1"))
        arc.store_conversation(
            Conversation(
                session_id="s1",
                user_id="u1",
                created_at=now,
                updated_at=now,
                last_accessed=now,
                status="active",
                messages=[Message(role="user", content="hi", timestamp=now)],
            )
        )
        arc.store_context(Context(domain="d1", created_at=now, updated_at=now))

        arc.clear_all()

        assert arc.get_invocation("t1") is None
        assert arc.get_conversation("s1") is None
        assert arc.get_context("d1") is None
        stats = arc.get_cache_stats()
        assert stats["disk_reads"] == 0
        assert stats["disk_writes"] == 0


class TestParseTimestamp:
    """Test _parse_timestamp static method."""

    def test_float_passthrough(self, arc):
        """Float timestamps should pass through."""
        assert ARCMemory._parse_timestamp(12345.0) == 12345.0

    def test_int_passthrough(self, arc):
        """Int timestamps should convert to float."""
        assert ARCMemory._parse_timestamp(12345) == 12345.0

    def test_iso_string_z(self, arc):
        """ISO 8601 string with Z should parse."""
        result = ARCMemory._parse_timestamp("2025-01-09T14:30:00Z")
        assert isinstance(result, float)
        assert result > 0

    def test_iso_string_offset(self, arc):
        """ISO 8601 string with offset should parse."""
        result = ARCMemory._parse_timestamp("2025-01-09T14:30:00+00:00")
        assert isinstance(result, float)
        assert result > 0

    def test_non_string_non_numeric_returns_current_time(self, arc):
        """Non-string, non-numeric should return current time."""
        result = ARCMemory._parse_timestamp(None)
        assert isinstance(result, float)
        assert result > 0
