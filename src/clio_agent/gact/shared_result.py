"""Single-flight, stale-while-revalidate results for polled read routes.

Clients poll a few routes whose answer costs a blocking collection: ``/v1/health``
runs the whole doctor, and ``/v1/mcp/servers`` resolves the session agent's plugins
and reads ``mcp.yaml`` files. Computed per request on the shared anyio pool, a
burst of polls ran N copies of the same collection at once. They competed for the
GIL with every other route, so each copy (and every unrelated request) slowed down
in proportion to the burst: 10-30 s answers under a mixed flood.

:class:`SharedResults` keeps the latest result per key and computes on its own
small executor, never on the shared pool:

* a result younger than ``fresh_s`` is returned as is;
* an older one starts at most one refresh per key. Callers keep getting the
  previous result until it is ``stale_s`` old, so polls do not wait for the
  collection;
* with no usable result, callers wait for the single in-flight computation;
* :meth:`SharedResults.invalidate` discards every result (and the result of a
  computation already running), so the next read reflects a change made just
  before it.

A failed computation is never stored: its waiters get the exception, and the
next read tries again.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from fastapi import FastAPI

T = TypeVar("T")

_CREATE_LOCK = threading.Lock()


@dataclass
class _Entry:
    value: Any = None
    has_value: bool = False
    at: float = 0.0
    inflight: concurrent.futures.Future[Any] | None = None


class SharedResults(Generic[T]):
    """The latest result of one blocking computation per key (see the module docstring)."""

    def __init__(
        self,
        name: str,
        *,
        fresh_s: float,
        stale_s: float,
        max_entries: int = 32,
        workers: int = 1,
    ) -> None:
        self.name = name
        self._fresh_s = fresh_s
        self._stale_s = max(stale_s, fresh_s)
        self._max_entries = max(1, max_entries)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix=f"gact-{name}"
        )
        self._lock = threading.RLock()
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._epoch = 0
        self._closed = False

    async def get(self, key: Hashable, compute: Callable[[], T]) -> T:
        """The result for ``key``, computing it with ``compute`` when needed."""

        with self._lock:
            entry = self._entry(key)
            age = time.monotonic() - entry.at
            if entry.has_value and age <= self._fresh_s:
                return entry.value
            future = entry.inflight
            if future is None:
                future = self._start(key, entry, compute)
            if entry.has_value and age <= self._stale_s:
                return entry.value
        # Shielded: a cancelled caller must not cancel the computation other callers share.
        return await asyncio.shield(asyncio.wrap_future(future))

    def prime(self, key: Hashable, value: T) -> None:
        """Store ``value`` for ``key`` as just computed (a collection made elsewhere)."""

        with self._lock:
            if self._closed:
                return
            entry = self._entry(key)
            entry.value = value
            entry.has_value = True
            entry.at = time.monotonic()

    def invalidate(self) -> None:
        """Discard every result; a computation already running is not stored."""

        with self._lock:
            self._epoch += 1
            self._entries.clear()

    def close(self) -> None:
        """Stop computing (shutdown). A computation already running finishes on its own."""

        with self._lock:
            self._closed = True
            self._entries.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _entry(self, key: Hashable) -> _Entry:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _Entry()
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        else:
            self._entries.move_to_end(key)
        return entry

    def _start(self, key: Hashable, entry: _Entry, compute: Callable[[], T]) -> Any:
        if self._closed:
            raise RuntimeError(f"{self.name} results are closed")
        epoch = self._epoch
        context = contextvars.copy_context()
        future = self._executor.submit(context.run, compute)
        entry.inflight = future
        future.add_done_callback(lambda done: self._settle(key, entry, done, epoch))
        return future

    def _settle(
        self, key: Hashable, entry: _Entry, done: concurrent.futures.Future[Any], epoch: int
    ) -> None:
        with self._lock:
            if entry.inflight is done:
                entry.inflight = None
            if done.cancelled() or done.exception() is not None:
                return
            if epoch == self._epoch and self._entries.get(key) is entry:
                entry.value = done.result()
                entry.has_value = True
                entry.at = time.monotonic()


def for_app(
    app: "FastAPI",
    name: str,
    *,
    fresh_s: float,
    stale_s: float,
    max_entries: int = 32,
    workers: int = 1,
) -> SharedResults[Any]:
    """The app's :class:`SharedResults` called ``name``, created on first use."""

    owners = getattr(app.state, "shared_results", None)
    if owners is None or name not in owners:
        with _CREATE_LOCK:
            owners = getattr(app.state, "shared_results", None)
            if owners is None:
                owners = app.state.shared_results = {}
            if name not in owners:
                owners[name] = SharedResults(
                    name,
                    fresh_s=fresh_s,
                    stale_s=stale_s,
                    max_entries=max_entries,
                    workers=workers,
                )
    return owners[name]


def invalidate(app: "FastAPI", name: str) -> None:
    """Discard the results of the app's ``name`` owner, if it exists."""

    owner = (getattr(app.state, "shared_results", None) or {}).get(name)
    if owner is not None:
        owner.invalidate()


def close_all(app: "FastAPI") -> None:
    """Close every owner of the app (shutdown)."""

    for owner in list((getattr(app.state, "shared_results", None) or {}).values()):
        owner.close()


__all__ = ["SharedResults", "close_all", "for_app", "invalidate"]
