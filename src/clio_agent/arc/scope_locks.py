"""Per-scope locks of the segment store, released with their scope (R6).

A scope's lock exists while the scope is cached or while a thread holds (or waits
on) it, and no longer: the registry would otherwise keep one lock for every scope
ever touched -- every transient read, every probed chunk, every released session --
for the life of the process.

:meth:`ScopeLockRegistry.handle` returns a :class:`ScopeLock` that resolves the
registry entry when it is acquired and pins it there until it is released, so an
entry is never removed while any thread holds or waits on it, and two threads can
never hold two different locks for one scope. An entry whose last pin goes away is
removed unless ``retain(key)`` says the scope is still cached; the cache's own
release drops the scope while holding its lock, so that release's unpin removes it.

The registry's guard is held only for dictionary bookkeeping, never while a thread
waits on a scope lock, so a thread holding one scope lock can always look up and take
another (lane writers do: a lane's base lock, then its chunks).

Locks that guard no cached scope (the fold's per-partition writer and per-session
index loader) are *pseudo-scopes* of the same registry (:func:`pseudo_scope`): bounded
the same way, and skipped by a release, which has nothing of theirs to drop.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import Optional

__all__ = ["ScopeKey", "ScopeLock", "ScopeLockRegistry", "is_pseudo_scope", "pseudo_scope"]

ScopeKey = tuple[str, str]

# No stored scope name starts with NUL; residency's invocation pseudo-scope uses it too.
_PSEUDO = "\x00"


def pseudo_scope(name: str) -> str:
    """The registry scope of a lock that guards no cached scope (``name`` is its role)."""
    return f"{_PSEUDO}{name}"


def is_pseudo_scope(scope: str) -> bool:
    """Whether ``scope`` is a :func:`pseudo_scope` (never cached)."""
    return scope.startswith(_PSEUDO)


class _Entry:
    __slots__ = ("lock", "pins")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pins = 0  # acquisitions held or waited on (re-entrant ones included)


class ScopeLock:
    """A handle on one scope's re-entrant lock (``acquire``/``release``/``with``).

    Handles are cheap and interchangeable: two handles on one scope compare equal and
    resolve to the same lock while either is held.
    """

    __slots__ = ("_key", "_registry")

    def __init__(self, registry: ScopeLockRegistry, key: ScopeKey) -> None:
        self._registry = registry
        self._key = key

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the scope's lock, pinning its registry entry first."""
        entry = self._registry._pin(self._key)
        acquired = False
        try:
            acquired = entry.lock.acquire(blocking, timeout)
        finally:
            if not acquired:  # refused, timed out or interrupted: nothing is held
                self._registry._unpin(self._key, entry)
        return acquired

    def release(self) -> None:
        """Release one acquisition; the last unpin of an uncached scope drops it.

        Raises:
            RuntimeError: This thread does not hold the scope's lock.
        """
        entry = self._registry._pinned(self._key)
        entry.lock.release()  # raises (and keeps the pin) when this thread is no holder
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

    def __repr__(self) -> str:
        return f"ScopeLock({self._key!r})"


class ScopeLockRegistry:
    """The ``(session_id, scope) -> lock`` registry, bounded by what is in use."""

    def __init__(self, retain: Callable[[ScopeKey], bool]) -> None:
        """``retain(key)``: whether the scope is cached (its lock then outlives pins).

        ``retain`` is called under the registry guard: it must not block or take a lock.
        """
        self._guard = threading.Lock()  # dict bookkeeping only; never held while waiting
        self._entries: dict[ScopeKey, _Entry] = {}
        self._retain = retain

    def handle(self, session_id: str, scope: str) -> ScopeLock:
        """The lock handle for ``(session_id, scope)``."""
        return ScopeLock(self, (session_id, scope))

    def keys(self, session_id: Optional[str] = None) -> list[ScopeKey]:
        """The scopes that have a lock now (every cached scope is among them), sorted."""
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
