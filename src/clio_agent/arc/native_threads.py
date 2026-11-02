"""The persistent OS threads that may enter the clio-core native runtime.

clio-core's client runtime (``iowarp_core``'s ``libclio_run_cxx``) gives every OS thread
that calls into it its own ``epoll`` instance and ``signalfd``, and releases neither when
that thread exits (measured: 20 calls on one new thread add one pair; 20 calls on 20 new
threads add 20 pairs; calls on the main thread add none). A caller-thread RPC therefore
leaks two descriptors for every distinct thread that ever runs one. Thread-pool workers
retire and are replaced (anyio's idle workers exit after 10 s; every ``asyncio.run`` has
its own default executor), so a long-lived server -- or a test worker late in the suite
-- climbs past ``FD_SETSIZE`` and toward the descriptor limit.

:func:`run_on_native_thread` keeps the set of native threads bounded by peak concurrency,
not by thread churn. A call from a thread that lives for the whole process (the main
thread, a stall-watch worker, a worker of this pool) runs where it is; any other thread
hands the call to an idle persistent worker here and blocks for its result, exactly as
it would have blocked running the call itself. The pool grows only when every worker is
busy, and its workers never exit, so each one's runtime resources are created once.

Nothing here adds a deadline: the calls routed here bound themselves
(:func:`clio_agent.arc.clio_core_async_ops.await_future` ends a stalled wait typed).
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TypeVar

__all__ = ["mark_native_thread", "native_thread_count", "run_on_native_thread"]

T = TypeVar("T")

_lock = threading.Lock()
_ready = threading.Condition(_lock)
_queue: list[tuple[Callable[[], object], Future[object]]] = []
_workers = 0
_idle = 0
_persistent = threading.local()


def mark_native_thread() -> None:
    """Declare the current thread process-lived, so its native calls run in place."""
    _persistent.native = True


def _is_native_thread() -> bool:
    return getattr(_persistent, "native", False) or threading.current_thread() is (
        threading.main_thread()
    )


def native_thread_count() -> int:
    """How many pool workers exist (each entered the native runtime at most once)."""
    with _lock:
        return _workers


def _worker() -> None:
    global _idle
    mark_native_thread()
    with _ready:
        _idle += 1
    while True:
        with _ready:
            while not _queue:
                _ready.wait()
            _idle -= 1
            fn, future = _queue.pop(0)
        if not future.set_running_or_notify_cancel():  # pragma: no cover - never cancelled
            with _ready:
                _idle += 1
            continue
        result: object = None
        error: BaseException | None = None
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 - forwards: to the caller's future
            error = exc
        # Available again BEFORE the caller resumes, so its next call (or the next
        # short-lived caller) finds this worker idle instead of growing the pool.
        with _ready:
            _idle += 1
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)


def run_on_native_thread(fn: Callable[[], T]) -> T:
    """Run ``fn`` (which enters clio-core) on a process-lived thread; return its result.

    Runs ``fn`` in a copy of the caller's context. Its exception, if any, is re-raised in
    the caller unchanged.
    """
    global _workers
    if _is_native_thread():
        return fn()
    context = contextvars.copy_context()
    future: Future[object] = Future()
    with _ready:
        _queue.append((lambda: context.run(fn), future))
        if _idle < len(_queue):
            # Daemon: a worker parked in a hung native call must not block interpreter
            # exit (the same reason the stall-watch pool is not a ThreadPoolExecutor).
            _workers += 1
            threading.Thread(target=_worker, name="arc-native", daemon=True).start()
        _ready.notify()
    return future.result()  # type: ignore[return-value]
