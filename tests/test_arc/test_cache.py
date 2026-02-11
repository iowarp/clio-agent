"""
Tests for clio_agent.arc.cache module.

Tests LRUCache get/put, TTL expiration, invalidation, stats, and eviction.
"""

import time

from clio_agent.arc.cache import LRUCache


class TestLRUCache:
    """Test LRU cache operations."""

    def test_put_and_get(self):
        """Test basic put and get."""
        cache = LRUCache(capacity=10)
        cache.put("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_get_nonexistent(self):
        """Getting a non-existent key returns None."""
        cache = LRUCache(capacity=10)
        assert cache.get("nonexistent") is None

    def test_put_overwrite(self):
        """Overwriting a key updates the value."""
        cache = LRUCache(capacity=10)
        cache.put("key1", "old")
        cache.put("key1", "new")
        assert cache.get("key1") == "new"

    def test_ttl_valid(self):
        """Entry with future TTL should be accessible."""
        cache = LRUCache(capacity=10)
        cache.put("key1", "value1", ttl_seconds=3600)
        assert cache.get("key1") == "value1"

    def test_ttl_expired(self):
        """Entry with expired TTL should return None."""
        cache = LRUCache(capacity=10)
        cache.put("key1", "value1", ttl_seconds=0)
        time.sleep(0.01)
        assert cache.get("key1") is None

    def test_invalidate(self):
        """Invalidating a key removes it."""
        cache = LRUCache(capacity=10)
        cache.put("key1", "value1")
        cache.invalidate("key1")
        assert cache.get("key1") is None

    def test_invalidate_nonexistent(self):
        """Invalidating non-existent key should not raise."""
        cache = LRUCache(capacity=10)
        cache.invalidate("nonexistent")  # Should not raise

    def test_clear(self):
        """Clear removes all entries and resets stats."""
        cache = LRUCache(capacity=10)
        cache.put("key1", "value1")
        cache.put("key2", "value2")
        cache.get("key1")  # hit
        cache.clear()
        stats = cache.stats()
        assert stats["size"] == 0
        assert stats["hits"] == 0
        assert stats["misses"] == 0

    def test_stats_hit_rate(self):
        """Stats should accurately track hit rate."""
        cache = LRUCache(capacity=10)
        cache.put("key1", "value1")
        cache.get("key1")  # hit
        cache.get("key2")  # miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_stats_zero_requests(self):
        """Hit rate should be 0.0 with no requests."""
        cache = LRUCache(capacity=10)
        stats = cache.stats()
        assert stats["hit_rate"] == 0.0

    def test_stats_capacity(self):
        """Stats should report configured capacity."""
        cache = LRUCache(capacity=42)
        stats = cache.stats()
        assert stats["capacity"] == 42

    def test_stats_size(self):
        """Stats should report current size."""
        cache = LRUCache(capacity=10)
        cache.put("a", 1)
        cache.put("b", 2)
        stats = cache.stats()
        assert stats["size"] == 2

    def test_stats_ttl_entries(self):
        """Stats should report entries with TTL."""
        cache = LRUCache(capacity=10)
        cache.put("a", 1, ttl_seconds=3600)
        cache.put("b", 2)
        stats = cache.stats()
        assert stats["ttl_entries"] == 1

    def test_ttl_cleared_on_overwrite(self):
        """Overwriting without TTL clears previous TTL."""
        cache = LRUCache(capacity=10)
        cache.put("key1", "value1", ttl_seconds=3600)
        cache.put("key1", "value2")  # No TTL
        stats = cache.stats()
        assert stats["ttl_entries"] == 0

    def test_complex_values(self):
        """Cache should handle dict and list values."""
        cache = LRUCache(capacity=10)
        cache.put("dict", {"a": 1, "b": [2, 3]})
        cache.put("list", [1, 2, 3])
        assert cache.get("dict") == {"a": 1, "b": [2, 3]}
        assert cache.get("list") == [1, 2, 3]
