"""Additional ARC Memory tests for coverage of uncovered paths.

Covers: index lifecycle (no cap; release eviction), get_invocation (cache miss path),
get_session_invocations, store_dataset_profile, get_dataset_profile (disk fallback),
get_session_profiles, store_procedural_memory, get_procedural_memories, get_lsm_stats,
clear_cache, clear_all, _parse_timestamp (string timestamps).
"""

import time

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import (
    Conversation,
    DatasetProfile,
    Invocation,
    Message,
    ProceduralMemory,
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


class TestIndexLifecycle:
    """The invocation B-tree index has NO size cap — an arbitrary ceiling would
    silently fail large workloads (entries falling off the end). Memory is bounded
    by LIFECYCLE instead: ``release_session`` evicts a session's branches on
    end/delete, and the index is rebuildable from the durable record on restart."""

    def test_no_size_cap_keeps_every_entry_findable(self, arc):
        """Storing far more than any old ceiling keeps EVERY entry — nothing is dropped."""
        for i in range(500):
            arc._inv_index.insert((f"s{i}", float(i)), {"trace_id": f"t{i}"})
        assert len(arc._inv_index) == 500

    def test_release_session_evicts_only_that_sessions_branches(self, arc):
        """Lifecycle eviction: releasing a session drops ITS index branches; others stay."""
        for i in range(5):
            arc._inv_index.insert(("keep", float(i)), {"trace_id": f"k{i}"})
            arc._inv_index.insert(("drop", float(i)), {"trace_id": f"t{i}"})

        arc.release_session("drop")

        inv_keys = list(arc._inv_index.keys())
        assert all(k[0] != "drop" for k in inv_keys), "released session's branch survived"
        assert any(k[0] == "keep" for k in inv_keys), "other session was wrongly evicted"


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


class TestLsmStats:
    """Test get_lsm_stats."""

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
        arc.clear_all()

        assert arc.get_invocation("t1") is None
        assert arc.get_conversation("s1") is None
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
