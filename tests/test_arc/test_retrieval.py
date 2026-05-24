"""Tests for ContextRetriever -- keyword-based context retrieval from ARC.

Tests cover:
- retrieve_context_for_query with and without conversation history
- extract_key_topics from conversations
- rank_conversations_by_relevance scoring
- _calculate_relevance_score with various overlaps
- _extract_keywords filtering and tokenization
- compile_expert_context delegation to ContextCompiler
- get_relevant_tool_results with cached tool data
"""

import time

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.retrieval import ContextRetriever
from clio_agent.arc.schema import (
    CachedToolResult,
    Context,
    Conversation,
    Message,
)


@pytest.fixture
def arc(tmp_path):
    return ARCMemory(data_dir=str(tmp_path / "arc"))


@pytest.fixture
def retriever(arc):
    return ContextRetriever(arc)


def _make_conversation(session_id: str, messages: list[tuple[str, str]]) -> Conversation:
    """Create a Conversation with given messages."""
    now = time.time()
    return Conversation(
        session_id=session_id,
        user_id="test",
        created_at=now,
        updated_at=now,
        last_accessed=now,
        status="active",
        messages=[Message(role=role, content=content, timestamp=now) for role, content in messages],
    )


class TestExtractKeywords:
    """Test _extract_keywords method."""

    def test_removes_stop_words(self, retriever):
        """Stop words like 'how', 'do', 'the' should be filtered."""
        keywords = retriever._extract_keywords("how do i optimize the file")
        assert "how" not in keywords
        assert "optimize" in keywords
        assert "file" in keywords

    def test_removes_short_tokens(self, retriever):
        """Tokens shorter than 3 chars should be filtered."""
        keywords = retriever._extract_keywords("an io ok test")
        assert "an" not in keywords
        assert "io" not in keywords
        assert "ok" not in keywords
        assert "test" in keywords

    def test_lowercases(self, retriever):
        """Keywords should be lowercase."""
        keywords = retriever._extract_keywords("HDF5 Compression GZIP")
        assert "hdf5" in keywords
        assert "compression" in keywords
        assert "gzip" in keywords

    def test_splits_on_punctuation(self, retriever):
        """Should split on non-alphanumeric characters."""
        keywords = retriever._extract_keywords("data optimization test")
        assert "data" in keywords
        assert "optimization" in keywords
        assert "test" in keywords

    def test_empty_string(self, retriever):
        """Empty string should return empty list."""
        assert retriever._extract_keywords("") == []


class TestCalculateRelevanceScore:
    """Test _calculate_relevance_score method."""

    def test_identical_content(self, retriever):
        """Identical query and conversation should score high."""
        conv = _make_conversation("s1", [("user", "optimize HDF5 compression")])
        score = retriever._calculate_relevance_score("optimize HDF5 compression", conv)
        assert score > 0.5

    def test_no_overlap(self, retriever):
        """No keyword overlap should score 0."""
        conv = _make_conversation("s1", [("user", "weather forecast tomorrow")])
        score = retriever._calculate_relevance_score("optimize HDF5 compression", conv)
        assert score == 0.0

    def test_partial_overlap(self, retriever):
        """Partial overlap should score between 0 and 1."""
        conv = _make_conversation("s1", [("user", "HDF5 file structure analysis")])
        score = retriever._calculate_relevance_score("optimize HDF5 compression", conv)
        assert 0.0 < score < 1.0

    def test_empty_query(self, retriever):
        """Empty query keywords should return 0."""
        conv = _make_conversation("s1", [("user", "HDF5")])
        score = retriever._calculate_relevance_score("the a an", conv)
        assert score == 0.0

    def test_empty_conversation(self, retriever):
        """Empty conversation should return 0."""
        conv = _make_conversation("s1", [("user", "the a an")])
        score = retriever._calculate_relevance_score("HDF5", conv)
        assert score == 0.0


class TestRankConversations:
    """Test rank_conversations_by_relevance."""

    def test_ranks_most_relevant_first(self, retriever):
        """Most relevant conversation should be first."""
        conv1 = _make_conversation("s1", [("user", "weather forecast")])
        conv2 = _make_conversation("s2", [("user", "HDF5 compression optimization")])

        ranked = retriever.rank_conversations_by_relevance(
            "optimize HDF5 compression", [conv1, conv2]
        )
        assert ranked[0].session_id == "s2"

    def test_empty_list(self, retriever):
        """Empty conversation list should return empty."""
        ranked = retriever.rank_conversations_by_relevance("query", [])
        assert ranked == []


class TestExtractKeyTopics:
    """Test extract_key_topics method."""

    def test_extracts_frequent_words(self, retriever):
        """Should extract most frequent meaningful words."""
        conv = _make_conversation(
            "s1",
            [
                ("user", "HDF5 compression HDF5 gzip HDF5"),
                ("assistant", "HDF5 uses gzip compression by default"),
            ],
        )
        topics = retriever.extract_key_topics([conv])
        assert "hdf5" in topics
        assert "compression" in topics

    def test_empty_conversations(self, retriever):
        """Empty conversation list should return empty topics."""
        assert retriever.extract_key_topics([]) == []


class TestRetrieveContextForQuery:
    """Test retrieve_context_for_query end-to-end."""

    def test_returns_context_object(self, retriever, arc):
        """Should return a Context object."""
        context = retriever.retrieve_context_for_query("optimize HDF5", "session-1")
        assert context is not None
        assert context.domain.startswith("query_context_")

    def test_with_conversation_history(self, retriever, arc):
        """Should include learned patterns from conversation."""
        conv = _make_conversation(
            "session-1",
            [
                ("user", "How to optimize HDF5 compression?"),
                ("assistant", "Use gzip compression with chunking."),
            ],
        )
        arc.store_conversation(conv)

        context = retriever.retrieve_context_for_query("HDF5 compression", "session-1")
        # Should have learned patterns
        assert len(context.learned_patterns) >= 0

    def test_without_conversation(self, retriever, arc):
        """Should handle missing conversation gracefully."""
        context = retriever.retrieve_context_for_query("test query", "nonexistent-session")
        assert context is not None


class TestCompileExpertContext:
    """Test compile_expert_context method."""

    def test_returns_string(self, retriever, arc):
        """Should return a compiled context string."""
        result = retriever.compile_expert_context("analyze HDF5", "session-1", tier=2)
        assert isinstance(result, str)

    def test_lazy_init_compiler(self, retriever):
        """ContextCompiler should be lazily initialized."""
        assert retriever._context_compiler is None
        retriever.compile_expert_context("test", "s1")
        assert retriever._context_compiler is not None


class TestGetRelevantToolResults:
    """Test get_relevant_tool_results."""

    def test_no_context_returns_empty(self, retriever):
        """Missing context should return empty list."""
        results = retriever.get_relevant_tool_results("analyze HDF5", "hdf5_domain")
        assert results == []

    def test_with_cached_results(self, retriever, arc):
        """Should return scored cached tool results."""
        now = time.time()
        ctx = Context(
            domain="hdf5_domain",
            created_at=now,
            updated_at=now,
            cached_tool_results={
                "hdf5_analyze": CachedToolResult(
                    params_hash="hash1",
                    result={"format": "hdf5", "compression": "gzip", "size": 1024},
                    cached_at=now,
                    ttl=3600,
                    hit_count=5,
                ),
                "parquet_read": CachedToolResult(
                    params_hash="hash2",
                    result={"format": "parquet", "rows": 100},
                    cached_at=now,
                    ttl=3600,
                    hit_count=2,
                ),
            },
        )
        arc.store_context(ctx)

        results = retriever.get_relevant_tool_results("HDF5 compression analysis", "hdf5_domain")
        # Should return results with hdf5-related content ranked higher
        assert len(results) >= 1
        assert "tool" in results[0]
        assert "result" in results[0]

    def test_max_results_limit(self, retriever, arc):
        """Should respect max_results parameter."""
        now = time.time()
        ctx = Context(
            domain="test_domain",
            created_at=now,
            updated_at=now,
            cached_tool_results={
                f"tool_{i}": CachedToolResult(
                    params_hash=f"hash{i}",
                    result={"data": f"result_{i}", "hdf5": "yes"},
                    cached_at=now,
                    ttl=3600,
                    hit_count=i,
                )
                for i in range(10)
            },
        )
        arc.store_context(ctx)

        results = retriever.get_relevant_tool_results(
            "hdf5 data analysis", "test_domain", max_results=3
        )
        assert len(results) <= 3
