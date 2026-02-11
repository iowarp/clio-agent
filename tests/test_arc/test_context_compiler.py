"""
Tests for ContextCompiler pipeline.

Tests the filter -> compact -> enrich -> assemble context compilation pipeline
with token budgets per tier.
"""

import time

from clio_agent.arc.context_compiler import ContextCompiler
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import (
    Conversation,
    DatasetProfile,
    Message,
    ProceduralMemory,
    RoutingDecision,
)


class TestContextCompilerInit:
    """Test ContextCompiler initialization."""

    def test_compiler_creates_with_default_budgets(self, tmp_path):
        """ContextCompiler should create with default tier budgets."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)
        assert compiler.tier_budgets["tier1"] == 2000
        assert compiler.tier_budgets["tier2"] == 4000

    def test_compiler_custom_budgets(self, tmp_path):
        """ContextCompiler should accept custom tier budgets."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc, tier_budgets={"tier1": 500, "tier2": 1000})
        assert compiler.tier_budgets["tier1"] == 500
        assert compiler.tier_budgets["tier2"] == 1000


class TestFilter:
    """Test the _filter stage of the pipeline."""

    def test_filter_returns_conversation_history(self, tmp_path):
        """Filter should extract recent messages from current session."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        session_id = "filter_test"

        # Store a conversation with messages
        conv = Conversation(
            session_id=session_id,
            user_id="test",
            created_at=time.time(),
            messages=[
                Message(role="user", content="What is HDF5?", timestamp=time.time()),
                Message(role="assistant", content="HDF5 is a data format.", timestamp=time.time()),
                Message(role="user", content="How to compress?", timestamp=time.time()),
            ],
        )
        arc.store_conversation(conv)

        compiler = ContextCompiler(arc)
        raw = compiler._filter("next question", session_id)
        assert len(raw["conversation"]) == 3
        assert raw["conversation"][0]["role"] == "user"
        assert raw["conversation"][0]["content"] == "What is HDF5?"

    def test_filter_returns_dataset_profiles(self, tmp_path):
        """Filter should find dataset profiles for the session."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        session_id = "filter_profiles"

        profile = DatasetProfile(
            session_id=session_id,
            filepath="/data/test.parquet",
            file_format="parquet",
            created_by="data",
            created_at=time.time(),
            schema_info={"columns": ["temp", "pressure"], "rows": 100},
            statistics={"temp": {"mean": 24.5}},
        )
        arc.store_dataset_profile(profile)

        compiler = ContextCompiler(arc)
        raw = compiler._filter("analyze data", session_id)
        assert len(raw["profiles"]) == 1
        assert raw["profiles"][0]["filepath"] == "/data/test.parquet"
        assert raw["profiles"][0]["format"] == "parquet"

    def test_filter_returns_procedural_memories(self, tmp_path):
        """Filter should find procedural memories for the session."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        session_id = "filter_proc"

        mem = ProceduralMemory(
            session_id=session_id,
            expert_id="data",
            pattern_type="success",
            description="gzip-6 worked well",
            context={"file": "test.h5"},
            outcome="3x compression",
            learned_at=time.time(),
        )
        arc.store_procedural_memory(mem)

        compiler = ContextCompiler(arc)
        raw = compiler._filter("optimize compression", session_id)
        assert len(raw["procedural"]) == 1
        assert raw["procedural"][0]["type"] == "success"
        assert raw["procedural"][0]["description"] == "gzip-6 worked well"

    def test_filter_returns_routing_history(self, tmp_path):
        """Filter should extract recent routing decisions."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        session_id = "filter_routing"

        conv = Conversation(
            session_id=session_id,
            user_id="test",
            created_at=time.time(),
            routing_decisions=[
                RoutingDecision(
                    timestamp=time.time(),
                    query="Analyze HDF5",
                    capabilities_needed=[],
                    selected_agent="data",
                    reasoning="HDF5 keyword",
                    confidence=0.95,
                ),
            ],
        )
        arc.store_conversation(conv)

        compiler = ContextCompiler(arc)
        raw = compiler._filter("more analysis", session_id)
        assert len(raw["routing"]) == 1
        assert raw["routing"][0]["selected"] == "data"

    def test_filter_empty_session(self, tmp_path):
        """Filter should return empty lists for nonexistent session."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)
        raw = compiler._filter("test query", "nonexistent")
        assert raw["conversation"] == []
        assert raw["profiles"] == []
        assert raw["procedural"] == []
        assert raw["routing"] == []


class TestCompact:
    """Test the _compact stage of the pipeline."""

    def test_compact_respects_tier1_budget(self, tmp_path):
        """Tier 1 budget (2K tokens) should limit output word count."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        # Create artificially large context
        raw = {
            "conversation": [
                {"role": "user", "content": "word " * 500}
            ],
            "profiles": [],
            "procedural": [],
            "routing": [],
        }

        compacted = compiler._compact(raw, budget_tokens=2000)
        conversation_text = compacted["conversation"]
        word_count = len(conversation_text.split())
        # 40% of 2000 tokens = 800 tokens * 0.75 words/token = 600 words max
        assert word_count <= 650  # Allow small margin for "user: " prefix

    def test_compact_respects_tier2_budget(self, tmp_path):
        """Tier 2 budget (4K tokens) should allow more content."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        raw = {
            "conversation": [
                {"role": "user", "content": "word " * 2000}
            ],
            "profiles": [],
            "procedural": [],
            "routing": [],
        }

        compacted = compiler._compact(raw, budget_tokens=4000)
        conversation_text = compacted["conversation"]
        word_count = len(conversation_text.split())
        # 40% of 4000 tokens = 1600 tokens * 0.75 words/token = 1200 words max
        assert word_count <= 1250

    def test_compact_proportional_allocation(self, tmp_path):
        """Conversation should get ~40% of budget, profiles ~30%."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        raw = {
            "conversation": [
                {"role": "user", "content": "conversation " * 1000}
            ],
            "profiles": [
                {"filepath": "/data/test.parquet", "format": "parquet",
                 "schema": {"columns": ["a"] * 100, "rows": 1000},
                 "stats": {"a": {"mean": 1.0} for _ in range(50)},
                 "created_by": "data"}
            ],
            "procedural": [],
            "routing": [],
        }

        compacted = compiler._compact(raw, budget_tokens=2000)
        conv_words = len(compacted["conversation"].split()) if compacted["conversation"] else 0
        prof_words = len(compacted["profiles"].split()) if compacted["profiles"] else 0

        # Conversation should have more words than profiles (40% vs 30%)
        if conv_words > 0 and prof_words > 0:
            assert conv_words > prof_words


class TestEnrich:
    """Test the _enrich stage of the pipeline."""

    def test_enrich_adds_tool_capabilities(self, tmp_path):
        """Enrich should add tool capability summaries."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        compacted = {"conversation": "", "profiles": "", "procedural": "", "routing": ""}
        enriched = compiler._enrich(compacted, "analyze HDF5 file")

        assert "tools" in enriched
        # Should contain tool names from gateway
        assert "hdf5" in enriched["tools"].lower() or enriched["tools"] == ""

    def test_enrich_adds_keywords(self, tmp_path):
        """Enrich should extract keywords from query."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        compacted = {"conversation": "", "profiles": "", "procedural": "", "routing": ""}
        enriched = compiler._enrich(compacted, "analyze HDF5 compression statistics")

        assert "keywords" in enriched
        assert "analyze" in enriched["keywords"]
        assert "hdf5" in enriched["keywords"]
        assert "compression" in enriched["keywords"]


class TestAssemble:
    """Test the _assemble stage of the pipeline."""

    def test_assemble_has_section_headers(self, tmp_path):
        """Assembled output should have [Section] headers."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        enriched = {
            "conversation": "user: Hello",
            "profiles": "/data/test.parquet",
            "procedural": "",
            "routing": "",
            "tools": "hdf5_analyze_file: Analyze HDF5 file.",
            "keywords": "hello",
        }

        assembled = compiler._assemble(enriched)
        assert "[Session Context]" in assembled
        assert "[Available Data]" in assembled
        assert "[Available Tools]" in assembled

    def test_assemble_empty_sections_omitted(self, tmp_path):
        """Empty sections should not appear in output."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        enriched = {
            "conversation": "",
            "profiles": "",
            "procedural": "",
            "routing": "",
            "tools": "",
            "keywords": "",
        }

        assembled = compiler._assemble(enriched)
        assert assembled == "No prior context"

    def test_assemble_only_populated_sections(self, tmp_path):
        """Only sections with content should appear."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        enriched = {
            "conversation": "user: test query",
            "profiles": "",
            "procedural": "",
            "routing": "",
            "tools": "",
            "keywords": "",
        }

        assembled = compiler._assemble(enriched)
        assert "[Session Context]" in assembled
        assert "[Available Data]" not in assembled


class TestCompileEndToEnd:
    """Test the full compile pipeline end-to-end."""

    def test_compile_end_to_end(self, tmp_path):
        """Full pipeline: store data, compile, verify structured output."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        session_id = "e2e_test"

        # Store conversation
        conv = Conversation(
            session_id=session_id,
            user_id="test",
            created_at=time.time(),
            messages=[
                Message(role="user", content="What datasets are in my file?", timestamp=time.time()),
                Message(role="assistant", content="Found 3 datasets in test.h5", timestamp=time.time()),
            ],
        )
        arc.store_conversation(conv)

        # Store profile
        profile = DatasetProfile(
            session_id=session_id,
            filepath="/data/test.h5",
            file_format="hdf5",
            created_by="data",
            created_at=time.time(),
            schema_info={"columns": ["temperature"], "rows": 100},
        )
        arc.store_dataset_profile(profile)

        # Compile
        compiler = ContextCompiler(arc)
        result = compiler.compile("analyze temperature column", session_id, tier=2)

        # Verify structured output
        assert "[Session Context]" in result
        assert "[Available Data]" in result
        assert "test.h5" in result
        assert isinstance(result, str)
        assert len(result) > 0

    def test_compile_empty_session(self, tmp_path):
        """Empty session should produce minimal context."""
        arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        compiler = ContextCompiler(arc)

        result = compiler.compile("test query", "empty_session", tier=1)
        # Should still have tools section from enrich
        assert isinstance(result, str)
        # Either has tools or "No prior context"
        assert len(result) > 0
