"""Wait on an event loop without holding a fleet lock across the wait.

A sync tool executor runs its MCP work on the application loop and waits for it
(``SyncMCPToolExecutor``: setup, close, a namespace reset or stop). The workspace
fleet is resolved under the agent's shared registry lock, and that resolve builds,
reconciles and evicts executors. Code on the loop takes the same lock (a connector
probe listing the resident fleets), so a resolve waiting on the loop while it holds
the lock deadlocks the agent: the loop waits on the lock, the lock's holder waits on
the loop (W17 cold restart, 2026-11-25).

:func:`holding` takes such a lock for a critical section and registers it for the
thread; :func:`loop_result` releases every lock the thread registered while it
waits on a loop future and takes them again before returning, as
``await_source_policy_settled`` does while it sleeps. The critical section must stay
correct with the lock released at those points: ``resolve_workspace_executor``
serializes the resolves of one fleet on a per-fleet build lock that code on the loop
never takes.
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_local = threading.local()


def _held() -> list[threading.Lock]:
    held = getattr(_local, "locks", None)
    if held is None:
        held = []
        _local.locks = held
    return held


@contextmanager
def holding(lock: threading.Lock) -> Iterator[None]:
    """``with lock:``, released across this thread's :func:`loop_result` waits."""

    lock.acquire()
    held = _held()
    held.append(lock)
    try:
        yield
    finally:
        held.pop()
        lock.release()


def loop_result(future: concurrent.futures.Future[Any], timeout: float | None = None) -> Any:
    """``future.result(timeout)`` with the thread's :func:`holding` locks released meanwhile."""

    held = list(_held())
    for lock in reversed(held):
        lock.release()
    try:
        return future.result(timeout=timeout)
    finally:
        for lock in held:
            lock.acquire()


def holds_registered_lock() -> bool:
    """Whether this thread is inside a :func:`holding` section (test seam, traces)."""

    return bool(getattr(_local, "locks", None))


__all__ = ["holding", "holds_registered_lock", "loop_result"]
