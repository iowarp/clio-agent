"""Per-scope locks of the segment store, released with their scope (R6).

A scope's lock exists while the scope is cached or while a thread holds (or waits
on) it, and no longer: the registry would otherwise keep one lock for every scope
ever touched -- every transient read, every probed chunk -- for the life of the
process.

:meth:`ScopeLockRegistry.handle` returns a :class:`ScopeLock` that resolves the
registry entry when it is acquired and pins it there until it is released, so an
entry is never removed while any thread holds or waits on it, and two threads can
never hold two different locks for one scope. An entry whose last pin goes away is
removed unless ``retain(key)`` says the scope is still cached; the cache's own
release drops the scope while holding its lock, so that release's unpin removes it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import Optional

__all__ = ["ScopeLock", "ScopeLockRegistry"]

ScopeKey = tuple[str, str]


class _Entry:
    __slots__ = ("lock", "pins")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pins = 0  # acquisitions held or waited on (re-entrant ones included)


class ScopeLock:
    """A handle on one scope's re-entrant lock (``acquire``/``release``/``with``)."""

    __slots__ = ("_key", "_registry")

    def __init__(self, registry: "ScopeLockRegistry", key: ScopeKey) -> None:
        self._registry = registry
        self._key = key

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the scope's lock (pinning its entry first)."""
        entry = self._registry._pin(self._key)
        if entry.lock.acquire(blocking, timeout):
            return True
        self._registry._unpin(self._key, entry)
        return False

    def release(self) -> None:
        """Release one acquisition; the last unpin of an uncached scope drops it."""
        entry = self._registry._pinned(self._key)
        entry.lock.release()
        self._registry._unpin(self._key, entry)

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ScopeLock)
            and other._registry is self._registry
            and other._key == self._key
        )

    def __hash__(self) -> int:
        return hash((id(self._registry), self._key))


class ScopeLockRegistry:
    """The ``(session_id, scope) -> lock`` registry, bounded by what is in use."""

    def __init__(self, retain: Callable[[ScopeKey], bool]) -> None:
        """``retain(key)``: whether the scope is cached (its lock then outlives pins)."""
        self._guard = threading.Lock()  # held only for dict bookkeeping, never blocking
        self._entries: dict[ScopeKey, _Entry] = {}
        self._retain = retain

    def handle(self, session_id: str, scope: str) -> ScopeLock:
        """The lock handle for ``(session_id, scope)``."""
        return ScopeLock(self, (session_id, scope))

    def keys(self, session_id: Optional[str] = None) -> list[ScopeKey]:
        """The scopes that have a lock now (all cached scopes are among them)."""
        with self._guard:
            return sorted(k for k in self._entries if session_id is None or k[0] == session_id)

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)

    def _pin(self, key: ScopeKey) -> _Entry:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = _Entry()
            entry.pins += 1
            return entry

    def _pinned(self, key: ScopeKey) -> _Entry:
        with self._guard:
            entry = self._entries.get(key)
        if entry is None or entry.pins <= 0:
            raise RuntimeError(f"release of an unacquired scope lock {key!r}")
        return entry

    def _unpin(self, key: ScopeKey, entry: _Entry) -> None:
        with self._guard:
            entry.pins -= 1
            if entry.pins == 0 and self._entries.get(key) is entry and not self._retain(key):
                del self._entries[key]
