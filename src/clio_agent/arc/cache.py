"""LRU Cache for hot data (O(1) access)"""

import time
from threading import Lock
from typing import Any, Dict, Optional

try:
    from lru import LRU  # lru-dict package

    HAS_LRU_DICT = True
except ImportError:
    HAS_LRU_DICT = False


class LRUCache:
    """LRU cache with TTL support and hit rate tracking.

    Provides O(1) get/put operations with optional time-to-live expiration
    and thread-safe access. Tracks hit/miss statistics for performance monitoring.

    Args:
        capacity: Maximum number of items to store (default: 1000)

    Examples:
        >>> cache = LRUCache(capacity=100)
        >>> cache.put("key1", {"data": "value"}, ttl_seconds=3600)
        >>> result = cache.get("key1")
        >>> stats = cache.stats()
        >>> print(f"Hit rate: {stats['hit_rate']:.2%}")
    """

    def __init__(self, capacity: int = 1000):
        """Initialize LRU cache with given capacity.

        Args:
            capacity: Maximum number of items to cache
        """
        self._capacity = capacity
        self._lock = Lock()
        self._cache: Any

        # Use lru-dict if available, otherwise use dict with manual LRU
        if HAS_LRU_DICT:
            self._cache = LRU(capacity)
        else:
            self._cache = {}
            self._access_order: list[str] = []  # Track access order for manual LRU

        # TTL storage: key -> expiry_timestamp
        self._ttl: Dict[str, float] = {}

        # Stats tracking
        self._hits: int = 0
        self._misses: int = 0

    def get(self, key: str) -> Optional[Any]:
        """Retrieve value from cache if present and not expired.

        Args:
            key: Cache key to lookup

        Returns:
            Cached value if found and valid, None otherwise

        Examples:
            >>> cache.put("key1", "value1")
            >>> cache.get("key1")
            'value1'
            >>> cache.get("nonexistent")
            None
        """
        with self._lock:
            # Check if key exists
            if key not in self._cache:
                self._misses += 1
                return None

            # Check TTL expiry
            if key in self._ttl:
                if time.time() > self._ttl[key]:
                    # Expired - remove and count as miss
                    self._remove_key(key)
                    self._misses += 1
                    return None

            # Cache hit
            self._hits += 1
            value = self._cache[key]

            # Update access order for manual LRU
            if not HAS_LRU_DICT:
                if key in self._access_order:
                    self._access_order.remove(key)
                self._access_order.append(key)

            return value

    def put(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        """Store value in cache with optional TTL.

        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: Optional time-to-live in seconds

        Examples:
            >>> cache.put("key1", {"data": [1, 2, 3]})
            >>> cache.put("key2", "temp_value", ttl_seconds=300)
        """
        with self._lock:
            # Handle eviction for manual LRU
            if not HAS_LRU_DICT:
                if key not in self._cache and len(self._cache) >= self._capacity:
                    # Evict least recently used
                    if self._access_order:
                        lru_key = self._access_order.pop(0)
                        self._remove_key(lru_key)

            # Store value
            self._cache[key] = value

            # Update access order for manual LRU
            if not HAS_LRU_DICT:
                if key in self._access_order:
                    self._access_order.remove(key)
                self._access_order.append(key)

            # Set TTL if provided
            if ttl_seconds is not None:
                self._ttl[key] = time.time() + ttl_seconds
            elif key in self._ttl:
                # Clear old TTL if no new TTL provided
                del self._ttl[key]

    def invalidate(self, key: str) -> None:
        """Remove a specific key from cache.

        Args:
            key: Cache key to invalidate

        Examples:
            >>> cache.put("key1", "value1")
            >>> cache.invalidate("key1")
            >>> cache.get("key1")
            None
        """
        with self._lock:
            self._remove_key(key)

    def keys(self) -> list[str]:
        """Return a snapshot list of current cache keys.

        Examples:
            >>> cache.put("conv:s1", object())
            >>> "conv:s1" in cache.keys()
            True
        """
        with self._lock:
            return list(self._cache.keys())

    def invalidate_prefix(self, prefix: str) -> int:
        """Remove all entries whose key starts with ``prefix``.

        Used to release a session's hot data from the cache without disturbing
        other sessions (cache keys are namespaced, e.g. ``conv:<sid>``).

        Args:
            prefix: Key prefix to match.

        Returns:
            Number of entries removed.

        Examples:
            >>> cache.put("conv:s1", object())
            >>> cache.invalidate_prefix("conv:s1")
            1
        """
        with self._lock:
            matching = [key for key in list(self._cache.keys()) if key.startswith(prefix)]
            for key in matching:
                self._remove_key(key)
            return len(matching)

    def clear(self) -> None:
        """Clear all entries from cache and reset statistics.

        Examples:
            >>> cache.put("key1", "value1")
            >>> cache.clear()
            >>> cache.stats()['size']
            0
        """
        with self._lock:
            if HAS_LRU_DICT:
                self._cache.clear()
            else:
                self._cache.clear()
                self._access_order.clear()

            self._ttl.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dictionary containing:
                - hit_rate: Cache hit rate (0.0 to 1.0)
                - hits: Total cache hits
                - misses: Total cache misses
                - size: Current number of items
                - capacity: Maximum capacity
                - ttl_entries: Number of entries with TTL

        Examples:
            >>> cache = LRUCache(capacity=100)
            >>> cache.put("key1", "value1")
            >>> cache.get("key1")
            'value1'
            >>> stats = cache.stats()
            >>> stats['hit_rate']
            1.0
            >>> stats['size']
            1
        """
        with self._lock:
            # Clean up expired entries before returning stats
            self._cleanup_expired()

            total_requests = self._hits + self._misses
            hit_rate = self._hits / total_requests if total_requests > 0 else 0.0

            return {
                "hit_rate": hit_rate,
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._cache),
                "capacity": self._capacity,
                "ttl_entries": len(self._ttl),
            }

    def _cleanup_expired(self) -> None:
        """Remove expired TTL entries from cache and tracking structures.

        Performs active cleanup of entries whose TTL has expired.
        Must be called with lock held.

        This prevents unbounded growth of the _ttl dictionary and
        ensures memory is reclaimed from expired entries.

        Examples:
            >>> cache = LRUCache(capacity=100)
            >>> cache.put("key1", "value1", ttl_seconds=1)
            >>> time.sleep(2)
            >>> cache._cleanup_expired()  # Removes expired entry
        """
        now = time.time()
        # Find all expired keys
        expired_keys = [key for key, expiry in self._ttl.items() if now > expiry]

        # Remove each expired key
        for key in expired_keys:
            self._remove_key(key)

    def _remove_key(self, key: str) -> None:
        """Internal method to remove key from cache and TTL storage.

        Must be called with lock held.

        Args:
            key: Key to remove
        """
        if key in self._cache:
            del self._cache[key]

        if key in self._ttl:
            del self._ttl[key]

        if not HAS_LRU_DICT and key in self._access_order:
            self._access_order.remove(key)
