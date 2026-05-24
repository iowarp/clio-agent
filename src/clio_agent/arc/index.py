"""B-Tree index for O(log N) retrieval using sortedcontainers.SortedDict"""

from typing import Any, Iterator, List, Optional, Tuple

from sortedcontainers import SortedDict


class BTreeIndex:
    """
    B-tree index using SortedDict for O(log N) operations.

    Supports composite keys (session_id + timestamp) for efficient
    timestamp-based retrieval of conversations and metrics.

    Performance Targets:
        - insert: O(log N)
        - search: O(log N) < 10ms
        - range_query: O(log N + k) where k = result count
        - delete: O(log N)

    Examples:
        >>> index = BTreeIndex()
        >>> index.insert(("session_1", 1609459200.0), {"message": "Hello"})
        >>> result = index.search(("session_1", 1609459200.0))
        >>> print(result)
        {'message': 'Hello'}

        >>> # Range query for all session_1 messages
        >>> results = index.range_query(
        ...     ("session_1", 0.0),
        ...     ("session_1", float('inf'))
        ... )
    """

    def __init__(self) -> None:
        """Initialize empty B-tree index."""
        self._index: SortedDict = SortedDict()

    def insert(self, key: Tuple[str, float], value: Any) -> None:
        """
        Insert key-value pair into index.

        Args:
            key: Composite key (session_id, timestamp)
            value: Associated data (Conversation, Invocation, Metric, etc.)

        Time Complexity: O(log N)

        Examples:
            >>> index.insert(("session_1", 1609459200.0), {"data": "value"})
        """
        self._index[key] = value

    def search(self, key: Tuple[str, float]) -> Optional[Any]:
        """
        Search for exact key match.

        Args:
            key: Composite key (session_id, timestamp)

        Returns:
            Associated value if found, None otherwise

        Time Complexity: O(log N)

        Examples:
            >>> result = index.search(("session_1", 1609459200.0))
            >>> if result:
            ...     print(f"Found: {result}")
        """
        return self._index.get(key)

    def range_query(
        self, start_key: Tuple[str, float], end_key: Tuple[str, float], inclusive: bool = True
    ) -> List[Any]:
        """
        Retrieve all values within key range [start_key, end_key].

        Args:
            start_key: Lower bound (session_id, timestamp)
            end_key: Upper bound (session_id, timestamp)
            inclusive: Include end_key in results (default: True)

        Returns:
            List of values in range, sorted by key

        Time Complexity: O(log N + k) where k = result count

        Examples:
            >>> # Get all messages in session_1 between two timestamps
            >>> results = index.range_query(
            ...     ("session_1", 1609459200.0),
            ...     ("session_1", 1609462800.0)
            ... )

            >>> # Get all messages in session_1
            >>> results = index.range_query(
            ...     ("session_1", 0.0),
            ...     ("session_1", float('inf'))
            ... )
        """
        # irange returns keys, so we need to extract values
        return [
            self._index[k]
            for k in self._index.irange(start_key, end_key, inclusive=(True, inclusive))
        ]

    def range_query_keys(
        self, start_key: Tuple[str, float], end_key: Tuple[str, float], inclusive: bool = True
    ) -> List[Tuple[str, float]]:
        """
        Retrieve all keys within range [start_key, end_key].

        Useful for getting timestamps or checking existence without loading values.

        Args:
            start_key: Lower bound (session_id, timestamp)
            end_key: Upper bound (session_id, timestamp)
            inclusive: Include end_key in results (default: True)

        Returns:
            List of keys in range, sorted

        Time Complexity: O(log N + k)

        Examples:
            >>> keys = index.range_query_keys(
            ...     ("session_1", 0.0),
            ...     ("session_1", float('inf'))
            ... )
        """
        return list(self._index.irange(start_key, end_key, inclusive=(True, inclusive)))

    def range_query_items(
        self, start_key: Tuple[str, float], end_key: Tuple[str, float], inclusive: bool = True
    ) -> List[Tuple[Tuple[str, float], Any]]:
        """
        Retrieve all (key, value) pairs within range.

        Args:
            start_key: Lower bound (session_id, timestamp)
            end_key: Upper bound (session_id, timestamp)
            inclusive: Include end_key in results (default: True)

        Returns:
            List of (key, value) tuples in range

        Time Complexity: O(log N + k)

        Examples:
            >>> items = index.range_query_items(
            ...     ("session_1", 0.0),
            ...     ("session_1", float('inf'))
            ... )
            >>> for key, value in items:
            ...     print(f"{key}: {value}")
        """
        # Return (key, value) tuples for all keys in range
        return [
            (k, self._index[k])
            for k in self._index.irange(start_key, end_key, inclusive=(True, inclusive))
        ]

    def delete(self, key: Tuple[str, float]) -> bool:
        """
        Delete key from index.

        Args:
            key: Composite key (session_id, timestamp)

        Returns:
            True if key existed and was deleted, False otherwise

        Time Complexity: O(log N)

        Examples:
            >>> deleted = index.delete(("session_1", 1609459200.0))
            >>> if deleted:
            ...     print("Key deleted")
        """
        if key in self._index:
            del self._index[key]
            return True
        return False

    def __len__(self) -> int:
        """
        Return number of entries in index.

        Returns:
            Total number of key-value pairs

        Time Complexity: O(1)

        Examples:
            >>> count = len(index)
            >>> print(f"Index has {count} entries")
        """
        return len(self._index)

    def __contains__(self, key: Tuple[str, float]) -> bool:
        """
        Check if key exists in index.

        Args:
            key: Composite key (session_id, timestamp)

        Returns:
            True if key exists, False otherwise

        Time Complexity: O(log N)

        Examples:
            >>> if ("session_1", 1609459200.0) in index:
            ...     print("Key exists")
        """
        return key in self._index

    def clear(self) -> None:
        """
        Remove all entries from index.

        Time Complexity: O(1)

        Examples:
            >>> index.clear()
            >>> assert len(index) == 0
        """
        self._index.clear()

    def get_session_range(self, session_id: str) -> List[Any]:
        """
        Retrieve all values for a given session_id.

        Convenience method for common operation of getting all entries
        for a session across all timestamps.

        Args:
            session_id: Session identifier

        Returns:
            List of all values for session, sorted by timestamp

        Time Complexity: O(log N + k)

        Examples:
            >>> conversations = index.get_session_range("session_1")
        """
        return self.range_query((session_id, 0.0), (session_id, float("inf")))

    def get_latest_in_session(self, session_id: str, n: int = 1) -> List[Any]:
        """
        Get the n most recent entries for a session.

        Args:
            session_id: Session identifier
            n: Number of recent entries to retrieve (default: 1)

        Returns:
            List of n most recent values, in chronological order (oldest to newest)

        Time Complexity: O(log N + k) where k = n (not total entries)

        Note:
            Uses reverse iteration to efficiently retrieve only the n most recent
            entries without loading entire session history.

        Examples:
            >>> # Get last message in session
            >>> latest = index.get_latest_in_session("session_1", n=1)

            >>> # Get last 5 messages
            >>> recent = index.get_latest_in_session("session_1", n=5)
        """
        # Get keys in reverse order (most recent first)
        all_keys = list(
            self._index.irange((session_id, 0.0), (session_id, float("inf")), reverse=True)
        )
        # Take first n keys (the n most recent)
        latest_keys = all_keys[:n]
        # Return values in chronological order (oldest to newest)
        return [self._index[k] for k in reversed(latest_keys)]

    def bisect_left(self, key: Tuple[str, float]) -> int:
        """
        Find insertion point for key (leftmost position).

        Useful for determining position in sorted order without inserting.

        Args:
            key: Composite key (session_id, timestamp)

        Returns:
            Index where key would be inserted

        Time Complexity: O(log N)

        Examples:
            >>> pos = index.bisect_left(("session_1", 1609459200.0))
            >>> print(f"Key would be inserted at position {pos}")
        """
        return self._index.bisect_left(key)

    def bisect_right(self, key: Tuple[str, float]) -> int:
        """
        Find insertion point for key (rightmost position).

        Args:
            key: Composite key (session_id, timestamp)

        Returns:
            Index where key would be inserted (after existing equal keys)

        Time Complexity: O(log N)

        Examples:
            >>> pos = index.bisect_right(("session_1", 1609459200.0))
        """
        return self._index.bisect_right(key)

    def items(self) -> Iterator[Tuple[Tuple[str, float], Any]]:
        """
        Iterate over all (key, value) pairs in sorted order.

        Returns:
            Iterator of (key, value) tuples

        Examples:
            >>> for key, value in index.items():
            ...     print(f"{key}: {value}")
        """
        return iter(self._index.items())

    def keys(self) -> Iterator[Tuple[str, float]]:
        """
        Iterate over all keys in sorted order.

        Returns:
            Iterator of keys

        Examples:
            >>> for key in index.keys():
            ...     print(key)
        """
        return iter(self._index.keys())

    def values(self) -> Iterator[Any]:
        """
        Iterate over all values in key-sorted order.

        Returns:
            Iterator of values

        Examples:
            >>> for value in index.values():
            ...     print(value)
        """
        return iter(self._index.values())
