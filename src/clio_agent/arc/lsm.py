"""LSM Tree for write-heavy metrics collection (v0.3.0)

This module implements a Log-Structured Merge (LSM) tree optimized for
high-throughput metrics collection with background compaction.

Architecture:
    - MemTable: In-memory SortedDict for recent writes (O(log N) inserts)
    - SSTables: Immutable on-disk sorted tables (msgpack format)
    - Background Compaction: Async thread merges SSTables to reduce read amplification

Performance Targets:
    - Write throughput > 1000 ops/sec
    - Read latency < 10ms (MemTable + SSTable scan)
    - Background compaction to manage disk usage

See PLAN.md v0.3.0 Task 3 for implementation requirements.
"""

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import msgspec
from sortedcontainers import SortedDict


@dataclass
class SSTable:
    """Sorted String Table metadata.

    Attributes:
        file_path: Path to SSTable file on disk
        min_key: Minimum timestamp in table (for range queries)
        max_key: Maximum timestamp in table (for range queries)
        record_count: Number of records in table

    Example:
        >>> sst = SSTable(
        ...     file_path=Path(".clio_agent/arc/lsm/sst_1234567890.msgpack"),
        ...     min_key=1704800000.0,
        ...     max_key=1704900000.0,
        ...     record_count=1000
        ... )
    """

    file_path: Path
    min_key: float
    max_key: float
    record_count: int


class LSMTree:
    """Log-Structured Merge tree for high-throughput metrics.

    Optimized for write-heavy workloads with background compaction.
    Uses in-memory MemTable for recent writes, flushes to immutable
    SSTables on disk, and periodically compacts SSTables.

    Args:
        data_dir: Directory for SSTables (default: ".clio_agent/arc/lsm")
        memtable_size: Max entries in MemTable before flush (default: 1000)
        compaction_threshold: Trigger compaction after N SSTables (default: 5)

    Examples:
        >>> lsm = LSMTree()
        >>> lsm.write(time.time(), {"agent": "DataExpert", "latency_ms": 1234})
        >>> metric = lsm.read(time.time())
        >>> stats = lsm.get_stats()
        >>> print(f"Writes: {stats['write_count']}")
        >>> lsm.close()
    """

    def __init__(
        self,
        data_dir: str = ".clio_agent/arc/lsm",
        memtable_size: int = 1000,
        compaction_threshold: int = 5,
    ):
        """Initialize LSM tree.

        Args:
            data_dir: Directory path for SSTable storage
            memtable_size: Maximum MemTable entries before flush
            compaction_threshold: SSTable count to trigger compaction
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # MemTable: in-memory sorted map (timestamp -> metric)
        self._memtable: SortedDict = SortedDict()
        self._memtable_size = memtable_size

        # SSTables: on-disk sorted tables (newest first for read efficiency)
        self._sstables: List[SSTable] = []
        self._compaction_threshold = compaction_threshold

        # Thread safety
        self._lock = threading.Lock()

        # Statistics
        self._write_count = 0
        self._flush_count = 0
        self._compaction_count = 0

        # Load existing SSTables from disk
        self._load_sstables()

        # Background compaction thread. Flush-time compaction below keeps tests
        # and short-lived CLI/API runs deterministic; the thread is a safety net
        # for longer-running processes.
        self._stop_compaction = threading.Event()
        self._compaction_thread = threading.Thread(
            target=self._compact_background, daemon=True, name="LSMCompaction"
        )
        self._compaction_thread.start()

    def _new_sstable_path(self, prefix: str = "sst") -> Path:
        """Return a collision-resistant SSTable path.

        Windows clocks can return identical ``time_ns`` values for
        rapid back-to-back flushes. A random suffix prevents a later
        flush from overwriting an older table before compaction reads
        it.
        """

        return self.data_dir / f"{prefix}_{time.time_ns()}_{uuid.uuid4().hex[:8]}.msgpack"

    def write(self, timestamp: float, metric: Dict[str, Any]) -> None:
        """Write metric to LSM tree (O(log N) due to SortedDict).

        Adds metric to MemTable. If MemTable is full, flushes to SSTable.

        Args:
            timestamp: Metric timestamp (used as key)
            metric: Metric data dictionary

        Examples:
            >>> lsm = LSMTree()
            >>> lsm.write(1704800000.0, {"agent": "DataExpert", "latency_ms": 1500})
            >>> lsm.write(1704800001.0, {"agent": "Main", "latency_ms": 234})
        """
        old_memtable = None

        with self._lock:
            # Add to MemTable (O(log N) insert)
            self._memtable[timestamp] = metric
            self._write_count += 1

            # Check if flush needed - use double-buffering to avoid holding lock during I/O
            if len(self._memtable) >= self._memtable_size:
                old_memtable = self._memtable
                self._memtable = SortedDict()  # Swap to new MemTable

        # Flush outside lock to avoid blocking writes
        if old_memtable is not None:
            self._flush_memtable_to_sstable(old_memtable)

    def read(self, timestamp: float) -> Optional[Dict[str, Any]]:
        """Read metric by timestamp.

        Checks MemTable first, then scans SSTables (newest first).

        Args:
            timestamp: Metric timestamp

        Returns:
            Metric dict if found, None otherwise

        Examples:
            >>> lsm = LSMTree()
            >>> lsm.write(1704800000.0, {"latency_ms": 1500})
            >>> metric = lsm.read(1704800000.0)
            >>> metric["latency_ms"]
            1500
        """
        # Fast path: check MemTable
        with self._lock:
            if timestamp in self._memtable:
                return dict(self._memtable[timestamp])

            # Slow path: check SSTables (newest first). Keep the
            # compaction lock while reading table files so a concurrent
            # compaction cannot delete a file between selecting and
            # scanning it.
            for sstable in self._sstables:
                if timestamp < sstable.min_key or timestamp > sstable.max_key:
                    continue

                metric = self._read_from_sstable(sstable, timestamp)
                if metric is not None:
                    return metric

        return None

    def range_scan(self, start_ts: float, end_ts: float) -> List[Dict[str, Any]]:
        """Scan metrics in timestamp range.

        Merges results from MemTable and all relevant SSTables.

        Args:
            start_ts: Start timestamp (inclusive)
            end_ts: End timestamp (inclusive)

        Returns:
            List of metrics in range, sorted by timestamp

        Examples:
            >>> lsm = LSMTree()
            >>> lsm.write(1704800000.0, {"latency_ms": 1500})
            >>> lsm.write(1704800001.0, {"latency_ms": 1600})
            >>> lsm.write(1704800002.0, {"latency_ms": 1400})
            >>> results = lsm.range_scan(1704800000.0, 1704800001.5)
            >>> len(results)
            2
        """
        results: Dict[float, Dict[str, Any]] = {}

        # Scan MemTable
        with self._lock:
            for ts in self._memtable.irange(start_ts, end_ts):
                results[ts] = self._memtable[ts]

            # Scan SSTables under the same lock compaction uses, so
            # range scans cannot race with compaction deleting old
            # table files.
            for sstable in self._sstables:
                if end_ts < sstable.min_key or start_ts > sstable.max_key:
                    continue

                sstable_results = self._range_scan_sstable(sstable, start_ts, end_ts)
                for ts, metric in sstable_results.items():
                    # MemTable has priority (newer data)
                    if ts not in results:
                        results[ts] = metric

        # Return sorted by timestamp
        return [results[ts] for ts in sorted(results.keys())]

    def _flush_memtable_to_sstable(self, memtable: SortedDict) -> None:
        """Flush given MemTable to SSTable on disk.

        Creates new immutable SSTable file with MemTable entries.
        This method should be called without holding self._lock to avoid blocking writes.

        Args:
            memtable: SortedDict to flush (typically swapped-out old MemTable)
        """
        if not memtable:
            return

        # Generate SSTable filename with timestamp
        sstable_path = self._new_sstable_path()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Serialize MemTable to msgpack
        # Store as list of (timestamp, metric) tuples for efficiency
        entries = [(ts, metric) for ts, metric in memtable.items()]
        encoded = msgspec.msgpack.encode(entries)
        sstable_path.write_bytes(encoded)

        # Create SSTable metadata
        min_key = memtable.keys()[0]
        max_key = memtable.keys()[-1]
        record_count = len(memtable)

        sstable = SSTable(
            file_path=sstable_path,
            min_key=min_key,
            max_key=max_key,
            record_count=record_count,
        )

        # Add to SSTables list (newest first for read efficiency)
        with self._lock:
            self._sstables.insert(0, sstable)
            self._flush_count += 1
            if len(self._sstables) >= self._compaction_threshold:
                self._compact_sstables()

    def _flush_memtable(self) -> None:
        """Flush MemTable to SSTable on disk.

        Creates new immutable SSTable file with all MemTable entries.
        Clears MemTable after successful flush.

        Note: Should be called with self._lock held.
        """
        if not self._memtable:
            return

        # Generate SSTable filename with timestamp
        sstable_path = self._new_sstable_path()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Serialize MemTable to msgpack
        # Store as list of (timestamp, metric) tuples for efficiency
        entries = [(ts, metric) for ts, metric in self._memtable.items()]
        encoded = msgspec.msgpack.encode(entries)
        sstable_path.write_bytes(encoded)

        # Create SSTable metadata
        min_key = self._memtable.keys()[0]
        max_key = self._memtable.keys()[-1]
        record_count = len(self._memtable)

        sstable = SSTable(
            file_path=sstable_path,
            min_key=min_key,
            max_key=max_key,
            record_count=record_count,
        )

        # Add to SSTables list (newest first for read efficiency)
        self._sstables.insert(0, sstable)

        # Clear MemTable
        self._memtable.clear()
        self._flush_count += 1
        if len(self._sstables) >= self._compaction_threshold:
            self._compact_sstables()

    def _compact_background(self) -> None:
        """Background compaction thread.

        Periodically checks if compaction is needed and merges SSTables.
        Runs until stop signal is set.
        """
        while not self._stop_compaction.is_set():
            # Check every 5 seconds
            self._stop_compaction.wait(timeout=5.0)

            # Check if compaction needed
            with self._lock:
                if len(self._sstables) >= self._compaction_threshold:
                    try:
                        self._compact_sstables()
                    except Exception as e:
                        # Log error but don't crash thread
                        # In production, would use proper logging
                        print(f"LSM compaction error: {e}")

    def _compact_sstables(self) -> None:
        """Merge SSTables to reduce read amplification.

        Merges all SSTables into a single compacted SSTable.
        Removes duplicate entries (keeps newest).

        Note: Should be called with self._lock held.
        """
        if len(self._sstables) < 2:
            return

        # Merge all SSTables
        merged_data: Dict[float, Dict[str, Any]] = {}

        # Read all SSTables (oldest to newest, so newer overwrites older)
        for sstable in reversed(self._sstables):
            entries = self._read_sstable(sstable)
            for ts, metric in entries:
                merged_data[ts] = metric

        # Write merged data to new SSTable
        compacted_path = self._new_sstable_path("sst_compacted")

        sorted_entries = [(ts, merged_data[ts]) for ts in sorted(merged_data.keys())]
        if not sorted_entries:
            self._sstables = [sst for sst in self._sstables if sst.file_path.exists()]
            return

        encoded = msgspec.msgpack.encode(sorted_entries)
        compacted_path.write_bytes(encoded)

        # Create new SSTable metadata
        if sorted_entries:
            min_key = sorted_entries[0][0]
            max_key = sorted_entries[-1][0]
            record_count = len(sorted_entries)

            compacted_sstable = SSTable(
                file_path=compacted_path,
                min_key=min_key,
                max_key=max_key,
                record_count=record_count,
            )

            # Delete old SSTables
            for sstable in self._sstables:
                try:
                    sstable.file_path.unlink()
                except Exception:
                    pass  # Best effort deletion

            # Replace with compacted SSTable
            self._sstables = [compacted_sstable]
            self._compaction_count += 1

    def _read_from_sstable(self, sstable: SSTable, timestamp: float) -> Optional[Dict[str, Any]]:
        """Read single metric from SSTable.

        Args:
            sstable: SSTable to read from
            timestamp: Timestamp to lookup

        Returns:
            Metric dict if found, None otherwise
        """
        entries = self._read_sstable(sstable)
        for ts, metric in entries:
            if ts == timestamp:
                return metric
        return None

    def _range_scan_sstable(
        self, sstable: SSTable, start_ts: float, end_ts: float
    ) -> Dict[float, Dict[str, Any]]:
        """Scan SSTable for metrics in timestamp range.

        Args:
            sstable: SSTable to scan
            start_ts: Start timestamp (inclusive)
            end_ts: End timestamp (inclusive)

        Returns:
            Dict mapping timestamp to metric
        """
        results: Dict[float, Dict[str, Any]] = {}
        entries = self._read_sstable(sstable)

        for ts, metric in entries:
            if start_ts <= ts <= end_ts:
                results[ts] = metric

        return results

    def _read_sstable(self, sstable: SSTable) -> List[tuple[float, Dict[str, Any]]]:
        """Read all entries from SSTable.

        Args:
            sstable: SSTable to read

        Returns:
            List of (timestamp, metric) tuples
        """
        if not sstable.file_path.exists():
            return []

        encoded = sstable.file_path.read_bytes()
        entries: List[tuple[float, Dict[str, Any]]] = msgspec.msgpack.decode(encoded)
        return entries

    def _load_sstables(self) -> None:
        """Load existing SSTables from disk.

        Scans data directory for SSTable files and loads metadata.
        Called during initialization.
        """
        sstable_files = sorted(self.data_dir.glob("sst_*.msgpack"), reverse=True)

        for sstable_path in sstable_files:
            try:
                # Read SSTable to extract metadata
                encoded = sstable_path.read_bytes()
                entries = msgspec.msgpack.decode(encoded)

                if not entries:
                    continue

                min_key = entries[0][0]
                max_key = entries[-1][0]
                record_count = len(entries)

                sstable = SSTable(
                    file_path=sstable_path,
                    min_key=min_key,
                    max_key=max_key,
                    record_count=record_count,
                )

                self._sstables.append(sstable)

            except Exception:
                # Skip corrupted SSTables
                continue

    def get_stats(self) -> Dict[str, Any]:
        """Get LSM tree statistics.

        Returns:
            Dictionary containing:
                - write_count: Total writes
                - flush_count: Total MemTable flushes
                - compaction_count: Total compactions
                - memtable_size: Current MemTable entry count
                - sstable_count: Current SSTable count
                - total_records: Approximate total records (may include duplicates)

        Examples:
            >>> lsm = LSMTree()
            >>> lsm.write(time.time(), {"latency_ms": 1500})
            >>> stats = lsm.get_stats()
            >>> stats["write_count"]
            1
        """
        with self._lock:
            total_records = len(self._memtable)
            for sstable in self._sstables:
                total_records += sstable.record_count

            return {
                "write_count": self._write_count,
                "flush_count": self._flush_count,
                "compaction_count": self._compaction_count,
                "memtable_size": len(self._memtable),
                "sstable_count": len(self._sstables),
                "total_records": total_records,
            }

    def close(self) -> None:
        """Stop background compaction and close LSM tree.

        Flushes MemTable to disk and stops compaction thread.
        Should be called before process exit.

        Examples:
            >>> lsm = LSMTree()
            >>> lsm.write(time.time(), {"latency_ms": 1500})
            >>> lsm.close()
        """
        # Signal compaction thread to stop
        self._stop_compaction.set()

        # Wait for compaction thread to finish (max 5 seconds)
        if self._compaction_thread.is_alive():
            self._compaction_thread.join(timeout=5.0)

        # Flush any remaining data in MemTable
        with self._lock:
            if self._memtable:
                self._flush_memtable()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures proper cleanup."""
        self.close()
        return False
