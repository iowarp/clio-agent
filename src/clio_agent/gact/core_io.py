"""Execution resource for the existing confirmed Core writer, not a task engine."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

T = TypeVar("T")


def _remember_thread(threads: list[threading.Thread], started: threading.Event) -> None:
    threads.append(threading.current_thread())
    started.set()


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

    async def run(self, operation: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Await Core work with caller context, without blocking native notification I/O."""
        return await asyncio.wrap_future(self.submit(operation, *args, **kwargs))

    def submit(self, operation: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
        """Queue one Core operation without tying its lifetime to an asyncio waiter."""
        with self._lock:
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
            future = executor.submit(context.run, partial(operation, *args, **kwargs))
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
