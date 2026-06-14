"""Tests for ARC's memory lifecycle: session release and flush-and-release.

These guard the S3 heap-fix primitives -- releasing a closed session's hot
footprint (cache + indexes) without losing durable data, and a full
flush-and-release back to baseline for tests/memprof.
"""

import time

import pytest

from clio_agent.arc.cache import LRUCache
from clio_agent.arc.index import BTreeIndex
from clio_agent.arc.lsm import LSMTree
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Conversation, Invocation, Message


@pytest.fixture
def arc(tmp_path):
    return ARCMemory(data_dir=str(tmp_path / "arc"))


def _inv(trace_id, session_id="s1", agent_id="data"):
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


def _conv(session_id):
    now = time.time()
    return Conversation(
        session_id=session_id,
        user_id="u1",
        created_at=now,
        updated_at=now,
        last_accessed=now,
        status="active",
        messages=[Message(role="user", content="hi", timestamp=now)],
    )


# ---- LRUCache primitives ----


class TestCachePrefixInvalidation:
    def test_invalidate_prefix_removes_matching_only(self):
        cache = LRUCache(capacity=100)
        cache.put("conv:s1", object())
        cache.put("profile:s1:a", object())
        cache.put("profile:s1:b", object())
        cache.put("conv:s2", object())

        removed = cache.invalidate_prefix("profile:s1:")

        assert removed == 2
        assert cache.get("profile:s1:a") is None
        assert cache.get("conv:s1") is not None
        assert cache.get("conv:s2") is not None

    def test_invalidate_prefix_no_match(self):
        cache = LRUCache(capacity=10)
        cache.put("conv:s1", object())
        assert cache.invalidate_prefix("nope:") == 0
        assert cache.get("conv:s1") is not None

    def test_keys_snapshot(self):
        cache = LRUCache(capacity=10)
        cache.put("a", 1)
        cache.put("b", 2)
        assert set(cache.keys()) == {"a", "b"}


# ---- BTreeIndex primitives ----


class TestIndexDeleteSession:
    def test_delete_session_removes_only_that_session(self):
        index = BTreeIndex()
        index.insert(("s1", 1.0), {"trace_id": "t1"})
        index.insert(("s1", 2.0), {"trace_id": "t2"})
        index.insert(("s2", 1.0), {"trace_id": "t3"})

        removed = index.delete_session("s1")

        assert removed == 2
        assert len(index) == 1
        assert ("s2", 1.0) in index

    def test_delete_session_absent(self):
        index = BTreeIndex()
        index.insert(("s1", 1.0), {"trace_id": "t1"})
        assert index.delete_session("missing") == 0
        assert len(index) == 1


# ---- LSMTree flush ----


class TestLSMFlush:
    def test_flush_empties_memtable_and_persists(self, tmp_path):
        lsm = LSMTree(data_dir=str(tmp_path / "lsm"), memtable_size=1000)
        try:
            ts = time.time()
            lsm.write(ts, {"latency_ms": 1500})
            assert len(lsm._memtable) == 1

            lsm.flush()

            assert len(lsm._memtable) == 0
            # Data survives the flush (now in an SSTable on disk).
            assert lsm.read(ts) is not None
        finally:
            lsm.close()

    def test_flush_empty_is_noop(self, tmp_path):
        lsm = LSMTree(data_dir=str(tmp_path / "lsm"), memtable_size=1000)
        try:
            lsm.flush()  # must not raise
        finally:
            lsm.close()


# ---- ARCMemory.release_session ----


class TestReleaseSession:
    def test_release_evicts_hot_but_keeps_disk(self, arc):
        arc.store_conversation(_conv("s1"))
        arc.store_invocation(_inv("t1", session_id="s1"))
        arc.store_invocation(_inv("t2", session_id="s1"))
        # warm the invocation cache
        arc.get_invocation("t1")

        result = arc.release_session("s1")

        # cache: conv:s1 + 2 invocations resolved via index
        assert result["cache"] >= 1
        assert result["index"] == 3  # 1 conv + 2 inv index entries
        # Hot copies gone...
        assert len(arc._conv_index) == 0
        assert len(arc._inv_index) == 0
        # ...but durable records survive and re-load from disk.
        assert arc.get_conversation("s1") is not None
        assert arc.get_invocation("t1") is not None
        assert arc.get_invocation("t2") is not None

    def test_release_isolates_other_sessions(self, arc):
        arc.store_conversation(_conv("s1"))
        arc.store_conversation(_conv("s2"))
        arc.store_invocation(_inv("t1", session_id="s1"))
        arc.store_invocation(_inv("t2", session_id="s2"))

        arc.release_session("s1")

        # s2's index entries remain intact.
        assert len(arc._conv_index) == 1
        assert len(arc._inv_index) == 1
        assert arc.get_conversation("s2") is not None

    def test_release_unknown_session_is_safe(self, arc):
        result = arc.release_session("never-existed")
        assert result["index"] == 0


# ---- ARCMemory.flush_and_release ----


class TestFlushAndRelease:
    def test_flush_and_release_returns_to_baseline(self, arc):
        arc.store_conversation(_conv("s1"))
        arc.store_invocation(_inv("t1", session_id="s1"))
        arc.get_invocation("t1")  # warm cache

        arc.flush_and_release()

        stats = arc.get_cache_stats()
        assert stats["size"] == 0
        assert stats["conv_index_size"] == 0
        assert stats["inv_index_size"] == 0
        # Durable data still readable (re-populates cache on read).
        assert arc.get_conversation("s1") is not None
        assert arc.get_invocation("t1") is not None
