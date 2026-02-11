"""Tests for IOWarpCTEBackend -- local fallback storage with tier migration.

Tests cover:
- Initialization with local fallback (IOWarp not available)
- Write/read to warm, cold, archive tiers
- Tier migration (warm -> cold -> archive)
- Access metadata tracking
- Tier stats reporting
- Shutdown and metadata persistence
"""

import pytest

from clio_agent.arc.storage import IOWarpCTEBackend


@pytest.fixture
def backend(tmp_path):
    """Create IOWarpCTEBackend with local fallback."""
    return IOWarpCTEBackend(
        namespace="/test/arc",
        base_dir=str(tmp_path / "arc_storage"),
    )


class TestInitialization:
    """Test IOWarpCTEBackend initialization."""

    def test_creates_tier_directories(self, tmp_path):
        """Init should create warm, cold, archive subdirectories."""
        base = tmp_path / "storage"
        IOWarpCTEBackend(base_dir=str(base))
        assert (base / "warm").is_dir()
        assert (base / "cold").is_dir()
        assert (base / "archive").is_dir()

    def test_iowarp_not_available(self, backend):
        """IOWarp should not be available in test environment."""
        assert backend.iowarp_available is False

    def test_default_tier_policy(self, backend):
        """Default tier policy should have expected values."""
        assert backend.tier_policy["hot_to_warm"] == 1
        assert backend.tier_policy["warm_to_cold"] == 7
        assert backend.tier_policy["cold_to_archive"] == 30

    def test_custom_tier_policy(self, tmp_path):
        """Custom tier policy should override defaults."""
        policy = {"hot_to_warm": 2, "warm_to_cold": 14, "cold_to_archive": 60}
        backend = IOWarpCTEBackend(
            base_dir=str(tmp_path / "s"), tier_policy=policy
        )
        assert backend.tier_policy["warm_to_cold"] == 14

    def test_performance_counters_start_at_zero(self, backend):
        """Performance counters should initialize to zero."""
        assert backend._local_reads == 0
        assert backend._local_writes == 0
        assert backend._tier_migrations == 0


class TestWriteRead:
    """Test write and read operations."""

    def test_write_and_read_warm(self, backend):
        """Write to warm tier and read back."""
        data = b"hello warm tier"
        backend.write("test/file.msgpack", data, tier="warm")
        result = backend.read("test/file.msgpack")
        assert result == data

    def test_write_and_read_cold(self, backend):
        """Write to cold tier and read back."""
        data = b"cold data"
        backend.write("test/cold.msgpack", data, tier="cold")
        result = backend.read("test/cold.msgpack")
        assert result == data

    def test_write_and_read_archive(self, backend):
        """Write to archive tier and read back."""
        data = b"archive data"
        backend.write("test/archive.msgpack", data, tier="archive")
        result = backend.read("test/archive.msgpack")
        assert result == data

    def test_read_nonexistent_returns_none(self, backend):
        """Read of nonexistent key should return None."""
        result = backend.read("nonexistent/key.msgpack")
        assert result is None

    def test_write_increments_counter(self, backend):
        """Write should increment local_writes counter."""
        backend.write("test/a.msgpack", b"data")
        assert backend._local_writes == 1

    def test_read_increments_counter(self, backend):
        """Successful read should increment local_reads counter."""
        backend.write("test/a.msgpack", b"data")
        backend.read("test/a.msgpack")
        assert backend._local_reads == 1

    def test_read_searches_all_tiers(self, backend):
        """Read should search all tiers if metadata tier is wrong."""
        backend._write_local("test/find_me.msgpack", b"found", tier="cold")
        # Metadata says warm but file is in cold -- should still find it
        backend._access_metadata["test/find_me.msgpack"] = {
            "tier": "warm",
            "access_count": 1,
        }
        result = backend.read("test/find_me.msgpack")
        assert result == b"found"


class TestTierMigration:
    """Test tier migration."""

    def test_migrate_warm_to_cold(self, backend):
        """migrate_tier should move data from warm to cold."""
        backend.write("migrate.msgpack", b"migrate me", tier="warm")
        backend.migrate_tier("migrate.msgpack", "cold")

        # Data should be in cold
        cold_path = backend._cold_dir / "migrate.msgpack"
        assert cold_path.exists()
        assert cold_path.read_bytes() == b"migrate me"
        assert backend._tier_migrations == 1

    def test_migrate_cold_to_archive(self, backend):
        """migrate_tier should move data from cold to archive."""
        backend.write("old.msgpack", b"old data", tier="cold")
        backend.migrate_tier("old.msgpack", "archive")

        archive_path = backend._archive_dir / "old.msgpack"
        assert archive_path.exists()

    def test_migrate_removes_old_file(self, backend):
        """migrate_tier should remove file from old tier."""
        backend.write("cleanup.msgpack", b"data", tier="warm")
        warm_path = backend._warm_dir / "cleanup.msgpack"
        assert warm_path.exists()

        backend.migrate_tier("cleanup.msgpack", "cold")
        assert not warm_path.exists()

    def test_migrate_nonexistent_is_noop(self, backend):
        """Migrating nonexistent key should not crash."""
        backend.migrate_tier("ghost.msgpack", "archive")
        assert backend._tier_migrations == 0


class TestAccessMetadata:
    """Test access metadata tracking."""

    def test_write_creates_metadata(self, backend):
        """Write should create access metadata for the key."""
        backend.write("meta.msgpack", b"data")
        assert "meta.msgpack" in backend._access_metadata
        meta = backend._access_metadata["meta.msgpack"]
        assert meta["tier"] == "warm"
        assert meta["access_count"] >= 1

    def test_read_increments_access_count(self, backend):
        """Read should increment access count in metadata."""
        backend.write("count.msgpack", b"data")
        initial = backend._access_metadata["count.msgpack"]["access_count"]
        backend.read("count.msgpack")
        assert backend._access_metadata["count.msgpack"]["access_count"] > initial

    def test_save_and_load_metadata(self, backend, tmp_path):
        """Metadata should survive save/load cycle."""
        backend.write("persist.msgpack", b"data")
        backend._save_access_metadata()

        # Create new backend pointing to same dir
        backend2 = IOWarpCTEBackend(
            base_dir=str(tmp_path / "arc_storage"),
        )
        assert "persist.msgpack" in backend2._access_metadata

    def test_corrupted_metadata_loads_empty(self, backend):
        """Corrupted metadata file should result in empty dict."""
        backend._access_metadata_file.write_bytes(b"garbage data")
        result = backend._load_access_metadata()
        assert result == {}


class TestTierStats:
    """Test get_tier_stats reporting."""

    def test_stats_iowarp_not_available(self, backend):
        """Stats should report iowarp_available=False in test env."""
        stats = backend.get_tier_stats()
        assert stats["iowarp_available"] is False
        assert stats["using_local_storage"] is True

    def test_stats_counts_files(self, backend):
        """Stats should count msgpack files per tier."""
        backend.write("a.msgpack", b"data", tier="warm")
        backend.write("b.msgpack", b"data", tier="cold")
        stats = backend.get_tier_stats()
        assert stats["tiers"]["warm"]["count"] == 1
        assert stats["tiers"]["cold"]["count"] == 1
        assert stats["tiers"]["archive"]["count"] == 0

    def test_stats_performance_counters(self, backend):
        """Stats should include performance counters."""
        backend.write("x.msgpack", b"data")
        backend.read("x.msgpack")
        stats = backend.get_tier_stats()
        assert stats["performance"]["local_writes"] >= 1
        assert stats["performance"]["local_reads"] >= 1


class TestShutdown:
    """Test shutdown."""

    def test_shutdown_saves_metadata(self, backend):
        """Shutdown should save access metadata to disk."""
        backend.write("shutdown.msgpack", b"data")
        backend.shutdown()
        assert backend._access_metadata_file.exists()


class TestGetTierDirectory:
    """Test _get_tier_directory method."""

    def test_warm_tier(self, backend):
        """Warm tier should return warm directory."""
        assert backend._get_tier_directory("warm") == backend._warm_dir

    def test_cold_tier(self, backend):
        """Cold tier should return cold directory."""
        assert backend._get_tier_directory("cold") == backend._cold_dir

    def test_archive_tier(self, backend):
        """Archive tier should return archive directory."""
        assert backend._get_tier_directory("archive") == backend._archive_dir

    def test_unknown_tier_defaults_to_warm(self, backend):
        """Unknown tier should default to warm directory."""
        assert backend._get_tier_directory("unknown") == backend._warm_dir
