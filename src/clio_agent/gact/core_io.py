"""Execution resource for the existing confirmed Core writer, not a task engine.

Operations run one at a time, in submission order (FIFO). :meth:`CoreIO.submit_batch`
lets one owner coalesce its queued items into one operation (group commit) without
reordering them against anything else: an item joins the queued batch only while no
other operation was submitted after that batch and the batch has not started.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any, Generic, TypeVar

from clio_agent.runtime import io_audit

T = TypeVar("T")
I = TypeVar("I")  # noqa: E741 - a batch item


def _remember_thread(threads: list[threading.Thread], started: threading.Event) -> None:
    threads.append(threading.current_thread())
    started.set()


def _timed(operation: Callable[[], T], queued: float) -> Callable[[], T]:
    """``operation``, reporting its queue wait and run time (``runtime.io_audit``)."""

    def run() -> T:
        started = time.perf_counter()
        try:
            return operation()
        finally:
            label = getattr(operation, "__qualname__", "") or type(operation).__name__
            io_audit.job(label, started - queued, time.perf_counter() - started)

    return run


class _Batch(Generic[I]):
    """Items queued for one ``runner`` call; closed to new items once it starts."""

    def __init__(self, io: CoreIO, runner: Callable[[list[I]], None], item: I) -> None:
        self.io, self.runner, self.items, self.started = io, runner, [item], False

    def run(self) -> None:
        with self.io._lock:
            self.started = True
            if self.io._tail is self:
                self.io._tail = None
        self.runner(self.items)


class CoreIO:
    """Keep blocking Core calls out of native transports' shared I/O executor.

    Core records and the confirmed writer retain all authority. This owns only
    one lazily started I/O thread; data-only history views start no worker.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._threads: list[threading.Thread] = []
        self._started = threading.Event()
        self._submitted = False
        self._finalizer: weakref.finalize | None = None
        self._closed = False
        # The last submitted operation when it is a batch that has not started.
        self._tail: _Batch[Any] | None = None

    async def run(self, operation: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Await Core work with caller context, without blocking native notification I/O."""
        return await asyncio.wrap_future(self.submit(operation, *args, **kwargs))

    def submit(self, operation: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
        """Queue one Core operation without tying its lifetime to an asyncio waiter."""
        with self._lock:
            self._tail = None  # later batch items must not run before this operation
            return self._submit(partial(operation, *args, **kwargs))

    def submit_batch(self, runner: Callable[[list[I]], None], item: I) -> None:
        """Queue ``item`` for ``runner``, in one call with the items queued just before it.

        The item joins the last queued operation when that is a batch for the same
        ``runner`` that has not started; otherwise a new batch is queued. Order is FIFO
        either way. ``runner`` gets the items in queue order and owns their outcomes
        (each item carries its own completion); it runs in the first item's context.
        """
        with self._lock:
            tail = self._tail
            if tail is not None and tail.runner == runner and not tail.started:
                tail.items.append(item)
                return
            batch = _Batch(self, runner, item)
            self._submit(batch.run)
            self._tail = batch

    def _submit(self, operation: Callable[[], T]) -> Future[T]:
        """Queue ``operation`` (the caller holds ``_lock``)."""
        if self._closed:
            raise RuntimeError("Core I/O resource is closed")
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="clio-core-io",
                initializer=_remember_thread,
                initargs=(self._threads, self._started),
            )
            # Application shutdown closes explicitly. Unmanaged data-reader
            # instances also release an idle thread when their view is collected.
            self._finalizer = weakref.finalize(self, self._executor.shutdown, wait=False)
        executor = self._executor
        context = contextvars.copy_context()
        future = executor.submit(context.run, _timed(operation, time.perf_counter()))
        self._submitted = True
        return future

    async def close(self) -> None:
        """Drain submitted I/O and retire its thread without borrowing another executor."""
        with self._lock:
            self._closed = True
            executor = self._executor
            if executor is None:
                return
            executor.shutdown(wait=False, cancel_futures=False)
        cancellation: asyncio.CancelledError | None = None
        while (self._submitted and not self._started.is_set()) or any(
            thread.is_alive() for thread in self._threads
        ):
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as error:
                cancellation = error
        if self._finalizer is not None:
            self._finalizer.detach()
        if cancellation is not None:
            raise cancellation
