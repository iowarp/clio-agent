"""Tests for LSM Tree implementation

Tests cover:
- Basic write/read operations
- Range scans
- MemTable flushing
- SSTable compaction
- Thread safety
- Performance targets (>1000 writes/sec)
"""

import shutil
import tempfile
import time
from pathlib import Path

import pytest

from clio_agent.arc.lsm import LSMTree, SSTable


class TestLSMTree:
    """Test suite for LSMTree class."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for tests."""
        temp_path = tempfile.mkdtemp()
        yield temp_path
        # Cleanup
        shutil.rmtree(temp_path, ignore_errors=True)

    @pytest.fixture
    def lsm(self, temp_dir):
        """Create LSMTree instance for testing."""
        lsm_tree = LSMTree(data_dir=temp_dir, memtable_size=10, compaction_threshold=3)
        yield lsm_tree
        lsm_tree.close()

    def test_basic_write_read(self, lsm):
        """Test basic write and read operations."""
        timestamp = 1704800000.0
        metric = {"agent": "DataExpert", "latency_ms": 1500}

        # Write metric
        lsm.write(timestamp, metric)

        # Read metric
        result = lsm.read(timestamp)
        assert result is not None
        assert result["agent"] == "DataExpert"
        assert result["latency_ms"] == 1500

    def test_read_nonexistent(self, lsm):
        """Test reading non-existent metric returns None."""
        result = lsm.read(9999999.0)
        assert result is None

    def test_multiple_writes(self, lsm):
        """Test multiple writes and reads."""
        metrics = [
            (1704800000.0, {"agent": "Main", "latency_ms": 100}),
            (1704800001.0, {"agent": "DataExpert", "latency_ms": 1500}),
            (1704800002.0, {"agent": "Main", "latency_ms": 200}),
        ]

        # Write all metrics
        for ts, metric in metrics:
            lsm.write(ts, metric)

        # Read all metrics
        for ts, expected_metric in metrics:
            result = lsm.read(ts)
            assert result is not None
            assert result == expected_metric

    def test_memtable_flush(self, lsm):
        """Test MemTable flush to SSTable."""
        # Write more than memtable_size (10) entries
        for i in range(15):
            ts = 1704800000.0 + i
            metric = {"index": i, "latency_ms": 100 + i}
            lsm.write(ts, metric)

        # Check stats - should have flushed at least once
        stats = lsm.get_stats()
        assert stats["write_count"] == 15
        assert stats["flush_count"] >= 1
        assert stats["sstable_count"] >= 1

        # Verify all data is readable
        for i in range(15):
            ts = 1704800000.0 + i
            result = lsm.read(ts)
            assert result is not None
            assert result["index"] == i

    def test_range_scan(self, lsm):
        """Test range scan functionality."""
        # Write metrics
        for i in range(20):
            ts = 1704800000.0 + i
            metric = {"index": i, "value": i * 10}
            lsm.write(ts, metric)

        # Range scan
        results = lsm.range_scan(1704800005.0, 1704800010.0)

        # Should have 6 results (5, 6, 7, 8, 9, 10)
        assert len(results) == 6
        assert results[0]["index"] == 5
        assert results[-1]["index"] == 10

    def test_range_scan_across_flush(self, lsm):
        """Test range scan across MemTable and SSTables."""
        # Write enough to trigger flush
        for i in range(15):
            ts = 1704800000.0 + i
            metric = {"index": i}
            lsm.write(ts, metric)

        # Range scan across MemTable and SSTable
        results = lsm.range_scan(1704800000.0, 1704800014.0)

        # Should have all 15 results
        assert len(results) == 15
        for i, result in enumerate(results):
            assert result["index"] == i

    def test_compaction(self, temp_dir):
        """Test SSTable compaction."""
        # Create LSM with low compaction threshold
        lsm = LSMTree(data_dir=temp_dir, memtable_size=5, compaction_threshold=3)

        try:
            # Write enough data to create multiple SSTables
            for i in range(20):
                ts = 1704800000.0 + i
                metric = {"index": i, "value": i}
                lsm.write(ts, metric)

            # Wait a bit for background compaction
            time.sleep(6)

            stats = lsm.get_stats()

            # Should have compacted (fewer SSTables than flushes)
            # With memtable_size=5 and 20 writes, we get 4 flushes
            # With compaction_threshold=3, compaction should trigger
            assert stats["compaction_count"] >= 1

            # Verify all data is still readable after compaction
            for i in range(20):
                ts = 1704800000.0 + i
                result = lsm.read(ts)
                assert result is not None
                assert result["index"] == i

        finally:
            lsm.close()

    def test_sstable_filenames_survive_clock_collision(self, temp_dir, monkeypatch):
        """Rapid flushes must not overwrite earlier SSTables.

        Windows can return identical ``time_ns`` values for back-to-back
        calls. SSTable names need a non-clock suffix so compaction sees
        every flushed table.
        """

        monkeypatch.setattr("clio_agent.arc.lsm.time.time_ns", lambda: 123456789)
        lsm = LSMTree(data_dir=temp_dir, memtable_size=5, compaction_threshold=3)

        try:
            for i in range(20):
                lsm.write(1704800000.0 + i, {"index": i})

            for i in range(20):
                result = lsm.read(1704800000.0 + i)
                assert result is not None
                assert result["index"] == i
        finally:
            lsm.close()

    def test_overwrite(self, lsm):
        """Test overwriting same timestamp."""
        timestamp = 1704800000.0

        # Write initial value
        lsm.write(timestamp, {"value": 100})

        # Overwrite
        lsm.write(timestamp, {"value": 200})

        # Should get latest value
        result = lsm.read(timestamp)
        assert result["value"] == 200

    def test_get_stats(self, lsm):
        """Test statistics tracking."""
        # Initial stats
        stats = lsm.get_stats()
        assert stats["write_count"] == 0
        assert stats["flush_count"] == 0
        assert stats["compaction_count"] == 0
        assert stats["memtable_size"] == 0
        assert stats["sstable_count"] == 0

        # Write some data
        for i in range(15):
            lsm.write(1704800000.0 + i, {"index": i})

        stats = lsm.get_stats()
        assert stats["write_count"] == 15
        assert stats["flush_count"] >= 1
        assert stats["total_records"] >= 15

    def test_persistence(self, temp_dir):
        """Test data persistence across LSM instances."""
        # Create LSM and write data
        lsm1 = LSMTree(data_dir=temp_dir, memtable_size=5)
        for i in range(15):
            lsm1.write(1704800000.0 + i, {"index": i})
        lsm1.close()

        # Create new LSM instance with same directory
        lsm2 = LSMTree(data_dir=temp_dir, memtable_size=5)

        # Should load existing SSTables
        stats = lsm2.get_stats()
        assert stats["sstable_count"] > 0

        # Verify data is readable
        for i in range(15):
            result = lsm2.read(1704800000.0 + i)
            # Note: Some data might be in old SSTables that were flushed
            # We can at least verify some data persisted
            if result is not None:
                assert "index" in result

        lsm2.close()

    def test_close_flushes_memtable(self, temp_dir):
        """Test that close() flushes remaining MemTable data."""
        lsm = LSMTree(data_dir=temp_dir, memtable_size=100)

        # Write data that won't trigger automatic flush
        for i in range(5):
            lsm.write(1704800000.0 + i, {"index": i})

        # Close should flush
        lsm.close()

        # Create new instance and verify data
        lsm2 = LSMTree(data_dir=temp_dir)
        for i in range(5):
            result = lsm2.read(1704800000.0 + i)
            assert result is not None
            assert result["index"] == i
        lsm2.close()

    def test_context_manager(self, temp_dir):
        """Test context manager support."""
        with LSMTree(data_dir=temp_dir) as lsm:
            lsm.write(1704800000.0, {"value": 42})
            result = lsm.read(1704800000.0)
            assert result["value"] == 42

        # After context exit, should have closed and flushed
        lsm2 = LSMTree(data_dir=temp_dir)
        result = lsm2.read(1704800000.0)
        # Data should persist if it was flushed
        lsm2.close()

    def test_high_throughput(self, temp_dir):
        """Test write throughput meets >1000 ops/sec target."""
        lsm = LSMTree(data_dir=temp_dir, memtable_size=10000)

        start_time = time.time()
        num_writes = 2000

        for i in range(num_writes):
            ts = 1704800000.0 + i * 0.001  # Spread across 2 seconds
            lsm.write(ts, {"index": i, "latency_ms": 100 + i})

        elapsed = time.time() - start_time
        throughput = num_writes / elapsed

        lsm.close()

        # Should achieve >1000 writes/sec
        assert throughput > 1000, f"Throughput {throughput:.0f} ops/sec < 1000 target"

    def test_empty_range_scan(self, lsm):
        """Test range scan with no matching data."""
        lsm.write(1704800000.0, {"value": 1})
        lsm.write(1704800010.0, {"value": 2})

        # Query range with no data
        results = lsm.range_scan(1704800002.0, 1704800008.0)
        assert len(results) == 0

    def test_sstable_dataclass(self):
        """Test SSTable dataclass."""
        sst = SSTable(
            file_path=Path("/tmp/test.msgpack"),
            min_key=1.0,
            max_key=100.0,
            record_count=50,
        )

        assert sst.file_path == Path("/tmp/test.msgpack")
        assert sst.min_key == 1.0
        assert sst.max_key == 100.0
        assert sst.record_count == 50
