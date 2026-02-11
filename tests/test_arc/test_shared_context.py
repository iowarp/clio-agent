"""
Tests for ARC shared context: DatasetProfile and ProceduralMemory.

Tests store/retrieve operations for cross-expert collaboration,
session isolation, disk persistence, ordering, and msgpack round-trips.
"""

import time

import msgspec
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import (
    DatasetProfile,
    ProceduralMemory,
    decode_dataset_profile,
    decode_procedural_memory,
    encode_dataset_profile,
    encode_procedural_memory,
)


@pytest.fixture
def arc(tmp_path):
    """Create an ARCMemory instance with isolated tmp_path storage."""
    return ARCMemory(data_dir=str(tmp_path / "arc"))


def _make_profile(
    session_id: str = "session-1",
    filepath: str = "/data/test.parquet",
    file_format: str = "parquet",
    created_by: str = "data",
    **kwargs,
) -> DatasetProfile:
    """Helper to create DatasetProfile with defaults."""
    return DatasetProfile(
        session_id=session_id,
        filepath=filepath,
        file_format=file_format,
        created_by=created_by,
        created_at=kwargs.get("created_at", time.time()),
        schema_info=kwargs.get("schema_info", {"columns": ["a", "b"], "rows": 100}),
        statistics=kwargs.get("statistics", {"a": {"mean": 1.0}}),
        quality_notes=kwargs.get("quality_notes", []),
        metadata=kwargs.get("metadata", {}),
    )


def _make_procedural(
    session_id: str = "session-1",
    expert_id: str = "data",
    pattern_type: str = "success",
    **kwargs,
) -> ProceduralMemory:
    """Helper to create ProceduralMemory with defaults."""
    return ProceduralMemory(
        session_id=session_id,
        expert_id=expert_id,
        pattern_type=pattern_type,
        description=kwargs.get("description", "test pattern"),
        context=kwargs.get("context", {}),
        outcome=kwargs.get("outcome", "test outcome"),
        learned_at=kwargs.get("learned_at", time.time()),
        confidence=kwargs.get("confidence", 0.8),
    )


class TestDatasetProfile:
    """Test DatasetProfile store/retrieve operations."""

    def test_store_and_get_dataset_profile(self, arc):
        """Store a profile and retrieve it by session + filepath."""
        profile = _make_profile()
        arc.store_dataset_profile(profile)

        result = arc.get_dataset_profile("session-1", "/data/test.parquet")
        assert result is not None
        assert result.session_id == "session-1"
        assert result.filepath == "/data/test.parquet"
        assert result.file_format == "parquet"
        assert result.created_by == "data"

    def test_get_dataset_profile_not_found(self, arc):
        """Returns None for nonexistent profile."""
        result = arc.get_dataset_profile("no-session", "/no/file.parquet")
        assert result is None

    def test_get_session_profiles_multiple(self, arc):
        """Store 3 profiles in same session, retrieve all."""
        arc.store_dataset_profile(_make_profile(filepath="/data/a.parquet"))
        arc.store_dataset_profile(_make_profile(filepath="/data/b.parquet"))
        arc.store_dataset_profile(
            _make_profile(filepath="/data/c.hdf5", file_format="hdf5")
        )

        profiles = arc.get_session_profiles("session-1")
        assert len(profiles) == 3
        filepaths = {p.filepath for p in profiles}
        assert "/data/a.parquet" in filepaths
        assert "/data/b.parquet" in filepaths
        assert "/data/c.hdf5" in filepaths

    def test_dataset_profile_cross_expert(self, arc):
        """DataExpert stores, AnalysisExpert retrieves by session."""
        # DataExpert stores profile
        profile = _make_profile(
            created_by="data",
            schema_info={"columns": ["temp", "pressure"], "rows": 1000},
            statistics={"temp": {"mean": 24.5, "std": 3.2}},
        )
        arc.store_dataset_profile(profile)

        # AnalysisExpert retrieves it
        result = arc.get_dataset_profile("session-1", "/data/test.parquet")
        assert result is not None
        assert result.created_by == "data"
        assert result.statistics["temp"]["mean"] == 24.5
        assert result.schema_info["rows"] == 1000

    def test_dataset_profile_different_sessions(self, arc):
        """Session A profiles are not visible to session B."""
        arc.store_dataset_profile(
            _make_profile(session_id="session-A", filepath="/data/a.parquet")
        )
        arc.store_dataset_profile(
            _make_profile(session_id="session-B", filepath="/data/b.parquet")
        )

        profiles_a = arc.get_session_profiles("session-A")
        profiles_b = arc.get_session_profiles("session-B")

        assert len(profiles_a) == 1
        assert profiles_a[0].filepath == "/data/a.parquet"
        assert len(profiles_b) == 1
        assert profiles_b[0].filepath == "/data/b.parquet"

        # Cross-session lookup returns None
        assert arc.get_dataset_profile("session-A", "/data/b.parquet") is None
        assert arc.get_dataset_profile("session-B", "/data/a.parquet") is None

    def test_dataset_profile_persistence(self, arc):
        """Store, clear cache, retrieve from disk."""
        profile = _make_profile()
        arc.store_dataset_profile(profile)

        # Clear cache
        arc.clear_cache()

        # Should still retrieve from disk
        result = arc.get_dataset_profile("session-1", "/data/test.parquet")
        assert result is not None
        assert result.filepath == "/data/test.parquet"


class TestProceduralMemory:
    """Test ProceduralMemory store/retrieve operations."""

    def test_store_and_get_procedural_memory(self, arc):
        """Store and retrieve a procedural memory."""
        mem = _make_procedural(
            description="gzip-6 works well on float64",
            pattern_type="success",
        )
        arc.store_procedural_memory(mem)

        memories = arc.get_procedural_memories("session-1")
        assert len(memories) == 1
        assert memories[0].description == "gzip-6 works well on float64"
        assert memories[0].pattern_type == "success"

    def test_procedural_memory_filter_by_expert(self, arc):
        """Filter procedural memories by expert_id."""
        arc.store_procedural_memory(
            _make_procedural(expert_id="data", description="data pattern")
        )
        arc.store_procedural_memory(
            _make_procedural(expert_id="analysis", description="analysis pattern")
        )

        data_mems = arc.get_procedural_memories("session-1", expert_id="data")
        analysis_mems = arc.get_procedural_memories("session-1", expert_id="analysis")

        assert len(data_mems) == 1
        assert data_mems[0].description == "data pattern"
        assert len(analysis_mems) == 1
        assert analysis_mems[0].description == "analysis pattern"

    def test_procedural_memory_ordering(self, arc):
        """Memories returned most recent first."""
        t1 = time.time() - 100
        t2 = time.time() - 50
        t3 = time.time()

        arc.store_procedural_memory(
            _make_procedural(description="oldest", learned_at=t1)
        )
        arc.store_procedural_memory(
            _make_procedural(description="middle", learned_at=t2)
        )
        arc.store_procedural_memory(
            _make_procedural(description="newest", learned_at=t3)
        )

        memories = arc.get_procedural_memories("session-1")
        assert len(memories) == 3
        assert memories[0].description == "newest"
        assert memories[1].description == "middle"
        assert memories[2].description == "oldest"

    def test_procedural_memory_limit(self, arc):
        """Limit caps number of returned memories."""
        for i in range(5):
            arc.store_procedural_memory(
                _make_procedural(
                    description=f"pattern-{i}",
                    learned_at=time.time() + i,
                )
            )

        memories = arc.get_procedural_memories("session-1", limit=2)
        assert len(memories) == 2


class TestSchemaRoundTrip:
    """Test msgpack encode/decode round-trips."""

    def test_dataset_profile_schema_encode_decode(self):
        """DatasetProfile survives msgpack round-trip."""
        profile = _make_profile(
            schema_info={"columns": ["x", "y"], "rows": 500},
            statistics={"x": {"mean": 10.0, "std": 2.5}},
            quality_notes=["5% nulls in column y"],
        )

        encoded = encode_dataset_profile(profile)
        assert isinstance(encoded, bytes)

        decoded = decode_dataset_profile(encoded)
        assert decoded.session_id == profile.session_id
        assert decoded.filepath == profile.filepath
        assert decoded.file_format == profile.file_format
        assert decoded.schema_info == profile.schema_info
        assert decoded.statistics == profile.statistics
        assert decoded.quality_notes == profile.quality_notes

    def test_procedural_memory_schema_encode_decode(self):
        """ProceduralMemory survives msgpack round-trip."""
        mem = _make_procedural(
            description="test round-trip",
            context={"key": "value"},
            outcome="success",
            confidence=0.95,
        )

        encoded = encode_procedural_memory(mem)
        assert isinstance(encoded, bytes)

        decoded = decode_procedural_memory(encoded)
        assert decoded.session_id == mem.session_id
        assert decoded.expert_id == mem.expert_id
        assert decoded.pattern_type == mem.pattern_type
        assert decoded.description == mem.description
        assert decoded.context == mem.context
        assert decoded.outcome == mem.outcome
        assert decoded.confidence == mem.confidence
