"""Core ARC API - Main interface for memory operations

This module provides the ARCMemory class which serves as the main interface
for all ARC (Adaptive Retrieval Cache) operations. It integrates:
    - LRUCache for hot data (O(1) access)
    - BTreeIndex for O(log N) retrieval
    - msgspec for efficient serialization
    - Thread-safe operations

Performance Targets:
    - Cache hit rate > 85%
    - Retrieval latency < 10ms
    - Thread-safe concurrent access

See docs/ARC_MEMORY_LAYER.md for architecture details.
"""

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from clio_agent.arc.cache import LRUCache
from clio_agent.arc.index import BTreeIndex
from clio_agent.arc.lsm import LSMTree
from clio_agent.arc.schema import (
    Context,
    Conversation,
    Invocation,
    Metrics,
    decode_context,
    decode_conversation,
    decode_invocation,
    decode_metrics,
    encode_context,
    encode_conversation,
    encode_invocation,
    encode_metrics,
)


class ARCMemory:
    """Adaptive Retrieval Cache - Main interface for memory operations.

    Provides cache-first storage and retrieval for conversations, invocations,
    metrics, and context with O(log N) fallback to disk.

    Args:
        data_dir: Directory for persistent storage (default: ".clio_agent/arc")
        cache_capacity: Maximum cache entries (default: 1000)

    Examples:
        >>> arc = ARCMemory()
        >>> conv = Conversation(session_id="session-1", ...)
        >>> arc.store_conversation(conv)
        >>> retrieved = arc.get_conversation("session-1")
        >>> stats = arc.get_cache_stats()
        >>> print(f"Hit rate: {stats['hit_rate']:.2%}")
    """

    def __init__(self, data_dir: str = ".clio_agent/arc", cache_capacity: int = 1000):
        """Initialize ARC memory system.

        Args:
            data_dir: Directory path for persistent storage
            cache_capacity: Maximum number of cached items
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Create subdirectories for different data types
        self._conv_dir = self.data_dir / "conversations"
        self._inv_dir = self.data_dir / "invocations"
        self._metrics_dir = self.data_dir / "metrics"
        self._context_dir = self.data_dir / "context"

        self._conv_dir.mkdir(exist_ok=True)
        self._inv_dir.mkdir(exist_ok=True)
        self._metrics_dir.mkdir(exist_ok=True)
        self._context_dir.mkdir(exist_ok=True)

        # Cache layer (hot data)
        self._cache = LRUCache(capacity=cache_capacity)

        # Index layers (O(log N) retrieval)
        # Keys are (session_id, timestamp) tuples
        self._conv_index = BTreeIndex()  # Conversation index
        self._inv_index = BTreeIndex()  # Invocation index

        # LSM tree for high-throughput metrics
        self._lsm = LSMTree(
            data_dir=str(self.data_dir / "lsm"),
            memtable_size=1000,
            compaction_threshold=5,
        )

        # Thread safety
        self._lock = threading.Lock()

        # Performance tracking
        self._disk_reads = 0
        self._disk_writes = 0

        # Index eviction configuration
        self._index_max_entries = 10000  # Maximum entries per index before eviction

    def _maybe_evict_index(self, index: BTreeIndex) -> None:
        """Evict old entries from index if it exceeds maximum size.

        Implements LRU-style eviction by removing oldest (first) entries
        when index grows beyond configured limit. This prevents unbounded
        memory growth in the B-tree indexes.

        Args:
            index: BTreeIndex instance to potentially evict from

        Examples:
            >>> arc = ARCMemory()
            >>> arc._maybe_evict_index(arc._conv_index)
        """
        if len(index) > self._index_max_entries:
            # Get all keys in sorted order
            all_keys = list(index.keys())

            # Calculate how many entries to remove (remove 10% to reduce churn)
            entries_to_remove = len(all_keys) - int(self._index_max_entries * 0.9)

            # Remove oldest entries (first in the sorted order)
            for key in all_keys[:entries_to_remove]:
                index.delete(key)

    def store_conversation(self, conversation: Conversation) -> None:
        """Store conversation in cache and index.

        Writes conversation to:
        1. In-memory cache (fast access)
        2. B-tree index (O(log N) lookup)
        3. Disk (persistent storage)

        Args:
            conversation: Conversation object to store

        Examples:
            >>> conv = Conversation(
            ...     session_id="session-1",
            ...     user_id="user@example.com",
            ...     created_at="2025-01-09T14:30:00Z",
            ...     updated_at="2025-01-09T14:30:00Z",
            ...     last_accessed="2025-01-09T14:30:00Z",
            ...     status="active"
            ... )
            >>> arc.store_conversation(conv)
        """
        with self._lock:
            session_id = conversation.session_id
            cache_key = f"conv:{session_id}"

            # Store in cache (hot data)
            self._cache.put(cache_key, conversation)

            # Store in index (for range queries)
            # Parse timestamp for index key
            timestamp = self._parse_timestamp(conversation.updated_at)
            index_key = (session_id, timestamp)
            self._conv_index.insert(index_key, {"session_id": session_id})

            # Persist to disk
            file_path = self._conv_dir / f"{session_id}.msgpack"
            encoded = encode_conversation(conversation)
            file_path.write_bytes(encoded)
            self._disk_writes += 1

            # Evict old index entries if necessary
            self._maybe_evict_index(self._conv_index)

    def get_conversation(self, session_id: str) -> Optional[Conversation]:
        """Retrieve conversation (cache-first).

        Lookup order:
        1. Check cache (O(1))
        2. Check disk (O(1) file read)

        Args:
            session_id: Session identifier

        Returns:
            Conversation object if found, None otherwise

        Examples:
            >>> conv = arc.get_conversation("session-1")
            >>> if conv:
            ...     print(f"Found {len(conv.messages)} messages")
        """
        cache_key = f"conv:{session_id}"

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: load from disk
        with self._lock:
            file_path = self._conv_dir / f"{session_id}.msgpack"
            if not file_path.exists():
                return None

            encoded = file_path.read_bytes()
            conversation = decode_conversation(encoded)
            self._disk_reads += 1

            # Update cache for future access
            self._cache.put(cache_key, conversation)

            return conversation

    def get_conversation_history(
        self, session_id: str, limit: int = 10
    ) -> List[Conversation]:
        """Get recent conversations for session.

        Retrieves the most recent conversation states (useful for seeing
        conversation evolution over time).

        Args:
            session_id: Session identifier
            limit: Maximum number of conversation states to retrieve

        Returns:
            List of Conversation objects, most recent first

        Examples:
            >>> history = arc.get_conversation_history("session-1", limit=5)
            >>> for conv in history:
            ...     print(f"Updated: {conv.updated_at}")
        """
        # For now, return single conversation if exists
        # In future, could track conversation snapshots over time
        conv = self.get_conversation(session_id)
        return [conv] if conv else []

    def store_invocation(self, invocation: Invocation) -> None:
        """Store agent invocation.

        Persists invocation trace to cache, index, and disk for later
        analysis and optimization.

        Args:
            invocation: Invocation object to store

        Examples:
            >>> inv = Invocation(
            ...     trace_id="trace-123",
            ...     session_id="session-1",
            ...     parent_trace_id=None,
            ...     agent_id="DataExpert",
            ...     tier=2,
            ...     source="native",
            ...     started_at="2025-01-09T14:30:00Z",
            ...     completed_at="2025-01-09T14:30:01Z",
            ...     duration_ms=1000,
            ...     status="success",
            ...     input={},
            ...     output={}
            ... )
            >>> arc.store_invocation(inv)
        """
        with self._lock:
            trace_id = invocation.trace_id
            session_id = invocation.session_id
            cache_key = f"inv:{trace_id}"

            # Store in cache
            self._cache.put(cache_key, invocation)

            # Store in index (for session-based queries)
            timestamp = self._parse_timestamp(invocation.started_at)
            index_key = (session_id, timestamp)
            self._inv_index.insert(index_key, {"trace_id": trace_id})

            # Persist to disk
            file_path = self._inv_dir / f"{trace_id}.msgpack"
            encoded = encode_invocation(invocation)
            file_path.write_bytes(encoded)
            self._disk_writes += 1

            # Also store in LSM tree for high-throughput metrics queries
            self._lsm.write(
                timestamp=timestamp,
                metric={
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "agent_id": invocation.agent_id,
                    "tier": invocation.tier,
                    "duration_ms": invocation.duration_ms,
                    "status": invocation.status,
                },
            )

            # Evict old index entries if necessary
            self._maybe_evict_index(self._inv_index)

    def get_invocation(self, invocation_id: str) -> Optional[Invocation]:
        """Get specific invocation.

        Args:
            invocation_id: Trace ID of invocation

        Returns:
            Invocation object if found, None otherwise

        Examples:
            >>> inv = arc.get_invocation("trace-123")
            >>> if inv:
            ...     print(f"Duration: {inv.duration_ms}ms")
        """
        cache_key = f"inv:{invocation_id}"

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: load from disk
        with self._lock:
            file_path = self._inv_dir / f"{invocation_id}.msgpack"
            if not file_path.exists():
                return None

            encoded = file_path.read_bytes()
            invocation = decode_invocation(encoded)
            self._disk_reads += 1

            # Update cache
            self._cache.put(cache_key, invocation)

            return invocation

    def get_session_invocations(
        self, session_id: str, limit: int = 100
    ) -> List[Invocation]:
        """Get invocations for a session.

        Retrieves invocation traces for analysis and debugging.

        Args:
            session_id: Session identifier
            limit: Maximum number of invocations to retrieve

        Returns:
            List of Invocation objects, most recent first

        Examples:
            >>> invocations = arc.get_session_invocations("session-1", limit=10)
            >>> for inv in invocations:
            ...     print(f"{inv.agent_id}: {inv.duration_ms}ms")
        """
        # Get all index entries for this session
        index_entries = self._inv_index.get_session_range(session_id)

        # Extract trace IDs and load invocations
        invocations = []
        for entry in index_entries[-limit:]:  # Get most recent
            trace_id = entry["trace_id"]
            inv = self.get_invocation(trace_id)
            if inv:
                invocations.append(inv)

        # Return most recent first
        return list(reversed(invocations))

    def store_metrics(self, metrics: Metrics) -> None:
        """Store performance metrics.

        Args:
            metrics: Metrics object to store

        Examples:
            >>> from clio_agent.arc.schema import (
            ...     InvocationStats, LatencyStats, UserSatisfactionStats
            ... )
            >>> metrics = Metrics(
            ...     agent_id="DataExpert",
            ...     tier=2,
            ...     period="2025-01",
            ...     computed_at="2025-01-31T23:59:59Z",
            ...     invocations=InvocationStats(100, 95, 5, 0, 0.95),
            ...     latency=LatencyStats(1500, 1200, 2500, 4000, 200, 8000),
            ...     user_satisfaction=UserSatisfactionStats(50, 45, 5, 0.90)
            ... )
            >>> arc.store_metrics(metrics)
        """
        with self._lock:
            agent_id = metrics.agent_id
            period = metrics.period
            cache_key = f"metrics:{agent_id}:{period}"

            # Store in cache
            self._cache.put(cache_key, metrics)

            # Persist to disk
            file_path = self._metrics_dir / f"{agent_id}_{period}.msgpack"
            encoded = encode_metrics(metrics)
            file_path.write_bytes(encoded)
            self._disk_writes += 1

    def get_metrics(
        self, agent_id: str, period: Optional[str] = None
    ) -> Optional[Metrics]:
        """Get metrics for agent.

        Args:
            agent_id: Agent identifier
            period: Time period (e.g., "2025-01"). If None, returns latest.

        Returns:
            Metrics object if found, None otherwise

        Examples:
            >>> metrics = arc.get_metrics("DataExpert", period="2025-01")
            >>> if metrics:
            ...     print(f"Success rate: {metrics.invocations.success_rate:.2%}")
        """
        # If no period specified, find latest metrics file
        if period is None:
            with self._lock:
                pattern = f"{agent_id}_*.msgpack"
                matching_files = sorted(self._metrics_dir.glob(pattern))
                if not matching_files:
                    return None
                # Get most recent file
                latest_file = matching_files[-1]
                encoded = latest_file.read_bytes()
                metrics = decode_metrics(encoded)
                self._disk_reads += 1

                # Cache it
                cache_key = f"metrics:{agent_id}:{metrics.period}"
                self._cache.put(cache_key, metrics)

                return metrics

        cache_key = f"metrics:{agent_id}:{period}"

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: load from disk
        with self._lock:
            file_path = self._metrics_dir / f"{agent_id}_{period}.msgpack"
            if not file_path.exists():
                return None

            encoded = file_path.read_bytes()
            metrics = decode_metrics(encoded)
            self._disk_reads += 1

            # Update cache
            self._cache.put(cache_key, metrics)

            return metrics

    def store_context(self, context: Context) -> None:
        """Store domain context.

        Args:
            context: Context object to store

        Examples:
            >>> ctx = Context(
            ...     domain="hdf5_optimization",
            ...     created_at="2025-01-09T14:30:00Z",
            ...     updated_at="2025-01-09T14:30:00Z"
            ... )
            >>> arc.store_context(ctx)
        """
        with self._lock:
            domain = context.domain
            cache_key = f"ctx:{domain}"

            # Store in cache
            self._cache.put(cache_key, context)

            # Persist to disk
            file_path = self._context_dir / f"{domain}.msgpack"
            encoded = encode_context(context)
            file_path.write_bytes(encoded)
            self._disk_writes += 1

    def get_context(self, domain: str) -> Optional[Context]:
        """Get context for domain.

        Args:
            domain: Domain identifier (e.g., "hdf5_optimization")

        Returns:
            Context object if found, None otherwise

        Examples:
            >>> ctx = arc.get_context("hdf5_optimization")
            >>> if ctx:
            ...     print(f"Cached tools: {len(ctx.cached_tool_results)}")
        """
        cache_key = f"ctx:{domain}"

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: load from disk
        with self._lock:
            file_path = self._context_dir / f"{domain}.msgpack"
            if not file_path.exists():
                return None

            encoded = file_path.read_bytes()
            context = decode_context(encoded)
            self._disk_reads += 1

            # Update cache
            self._cache.put(cache_key, context)

            return context

    def cache_tool_result(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any,
        ttl_seconds: int = 3600,
    ) -> None:
        """Cache tool result in ARC.

        Args:
            server_name: MCP server name (e.g., "hdf5")
            tool_name: Tool name (e.g., "analyze_file")
            arguments: Tool arguments dict
            result: Tool result to cache
            ttl_seconds: Cache TTL in seconds (default: 1 hour)

        Examples:
            >>> arc.cache_tool_result(
            ...     "hdf5",
            ...     "analyze_file",
            ...     {"path": "/data/experiment.h5"},
            ...     {"shape": [100, 200], "dtype": "float64"},
            ...     ttl_seconds=1800
            ... )
        """
        import hashlib
        import json

        # Create cache key from server + tool + args
        args_str = json.dumps(arguments, sort_keys=True)
        key_str = f"tool_{server_name}_{tool_name}_{args_str}"
        cache_key = hashlib.md5(key_str.encode()).hexdigest()

        # Store in cache with TTL
        self._cache.put(cache_key, result, ttl_seconds=ttl_seconds)

    def get_cached_tool_result(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Optional[Any]:
        """Get cached tool result from ARC.

        Args:
            server_name: MCP server name
            tool_name: Tool name
            arguments: Tool arguments dict

        Returns:
            Cached result or None if not found/expired

        Examples:
            >>> result = arc.get_cached_tool_result(
            ...     "hdf5",
            ...     "analyze_file",
            ...     {"path": "/data/experiment.h5"}
            ... )
            >>> if result is not None:
            ...     print(f"Cache hit: {result}")
        """
        import hashlib
        import json

        # Create same cache key
        args_str = json.dumps(arguments, sort_keys=True)
        key_str = f"tool_{server_name}_{tool_name}_{args_str}"
        cache_key = hashlib.md5(key_str.encode()).hexdigest()

        return self._cache.get(cache_key)

    def get_tool_cache_stats(self) -> Dict[str, Any]:
        """Get tool cache statistics.

        Returns:
            Dict with tool cache hit rate and counts

        Examples:
            >>> stats = arc.get_tool_cache_stats()
            >>> print(f"Tool cache hit rate: {stats['tool_cache_hit_rate']:.2%}")
            >>> print(f"Target: {stats['target_hit_rate']:.2%}")
        """
        stats = self._cache.stats()
        return {
            "tool_cache_hit_rate": stats["hit_rate"],
            "tool_cache_hits": stats["hits"],
            "tool_cache_misses": stats["misses"],
            "tool_cache_size": stats["size"],
            "target_hit_rate": 0.50,  # >50% per PLAN.md
        }

    def query_metrics_by_time_range(
        self, start_ts: float, end_ts: float
    ) -> List[Dict[str, Any]]:
        """Query metrics in time range using LSM tree.

        Provides fast time-range queries over invocation metrics
        stored in the LSM tree.

        Args:
            start_ts: Start timestamp (Unix timestamp)
            end_ts: End timestamp (Unix timestamp)

        Returns:
            List of metrics in range, sorted by timestamp

        Examples:
            >>> import time
            >>> start = time.time() - 3600  # Last hour
            >>> end = time.time()
            >>> metrics = arc.query_metrics_by_time_range(start, end)
            >>> for metric in metrics:
            ...     print(f"{metric['agent_id']}: {metric['duration_ms']}ms")
        """
        return self._lsm.range_scan(start_ts, end_ts)

    def get_lsm_stats(self) -> Dict[str, Any]:
        """Get LSM tree statistics.

        Returns:
            Dict with write throughput, compaction stats

        Examples:
            >>> stats = arc.get_lsm_stats()
            >>> print(f"LSM writes: {stats['write_count']}")
            >>> print(f"Flushes: {stats['flush_count']}")
            >>> print(f"Compactions: {stats['compaction_count']}")
        """
        return self._lsm.get_stats()

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache performance statistics.

        Returns:
            Dictionary containing:
                - hit_rate: Cache hit rate (0.0-1.0)
                - hits: Total cache hits
                - misses: Total cache misses
                - size: Current cache size
                - capacity: Maximum cache capacity
                - disk_reads: Total disk reads
                - disk_writes: Total disk writes
                - conv_index_size: Conversation index entry count
                - inv_index_size: Invocation index entry count

        Examples:
            >>> stats = arc.get_cache_stats()
            >>> print(f"Hit rate: {stats['hit_rate']:.2%}")
            >>> print(f"Disk reads: {stats['disk_reads']}")
        """
        with self._lock:
            cache_stats = self._cache.stats()

            return {
                "hit_rate": cache_stats["hit_rate"],
                "hits": cache_stats["hits"],
                "misses": cache_stats["misses"],
                "size": cache_stats["size"],
                "capacity": cache_stats["capacity"],
                "disk_reads": self._disk_reads,
                "disk_writes": self._disk_writes,
                "conv_index_size": len(self._conv_index),
                "inv_index_size": len(self._inv_index),
            }

    def clear_cache(self) -> None:
        """Clear in-memory cache (preserves disk storage).

        Useful for testing or memory pressure situations.

        Examples:
            >>> arc.clear_cache()
            >>> stats = arc.get_cache_stats()
            >>> assert stats['size'] == 0
        """
        self._cache.clear()

    def clear_all(self) -> None:
        """Clear all data (cache, indexes, and disk).

        WARNING: This deletes all persistent data. Use with caution.

        Examples:
            >>> arc.clear_all()  # Only use in tests or to reset state
        """
        with self._lock:
            # Clear cache
            self._cache.clear()

            # Clear indexes
            self._conv_index.clear()
            self._inv_index.clear()

            # Clear disk storage
            for file_path in self._conv_dir.glob("*.msgpack"):
                file_path.unlink()
            for file_path in self._inv_dir.glob("*.msgpack"):
                file_path.unlink()
            for file_path in self._metrics_dir.glob("*.msgpack"):
                file_path.unlink()
            for file_path in self._context_dir.glob("*.msgpack"):
                file_path.unlink()

            # Reset counters
            self._disk_reads = 0
            self._disk_writes = 0

    def __del__(self) -> None:
        """Cleanup LSM tree on delete."""
        if hasattr(self, "_lsm"):
            self._lsm.close()

    @staticmethod
    def _parse_timestamp(timestamp: float | str) -> float:
        """Parse timestamp to float.

        Args:
            timestamp: Float (Unix timestamp) or str (ISO 8601)

        Returns:
            Float timestamp

        Examples:
            >>> ts = ARCMemory._parse_timestamp("2025-01-09T14:30:00Z")
            >>> ts > 0
            True
            >>> ARCMemory._parse_timestamp(1736433000.0)
            1736433000.0
        """
        # If already float, return as-is
        if isinstance(timestamp, (int, float)):
            return float(timestamp)

        # Parse string timestamp (ISO 8601)
        if isinstance(timestamp, str):
            from datetime import datetime

            if timestamp.endswith("Z"):
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(timestamp)

            return dt.timestamp()

        # Fallback: return current timestamp
        import time

        return time.time()
