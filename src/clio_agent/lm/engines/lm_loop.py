"""The one event loop every agent-loop LM call runs on.

The agent loop is synchronous (tools run in worker threads), but DSPy's LM calls are
async. Running each call under its own ``asyncio.run`` closes that loop when the call
ends, and every async connection pool bound to it dies with it -- the next step pays a
new TCP + TLS handshake (and lm15's pooled async transport fails outright on a closed
loop). This module keeps ONE daemon event loop for the process: every call runs there as
a task in a copy of the caller's context (GACT session, turn, scope, cancellation), so
providers reuse their connections across steps, turns and agents.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

__all__ = ["run_on_lm_loop"]

T = TypeVar("T")

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None


def _loop() -> asyncio.AbstractEventLoop:
    global _LOOP  # noqa: PLW0603
    with _LOCK:
        if _LOOP is None or _LOOP.is_closed() or not _LOOP.is_running():
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            loop.call_soon(ready.set)
            threading.Thread(target=loop.run_forever, name="clio-lm-loop", daemon=True).start()
            ready.wait()
            _LOOP = loop
        return _LOOP


def run_on_lm_loop(make: Callable[[], Awaitable[T]]) -> T:
    """Run ``make()`` on the LM loop in a copy of the caller's context; block for it.

    Raises:
        RuntimeError: called from the LM loop itself (it would wait on itself).
    """
    loop = _loop()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        raise RuntimeError("run_on_lm_loop called from the LM loop (would deadlock)")
    context = contextvars.copy_context()
    result: concurrent.futures.Future[T] = concurrent.futures.Future()

    def start() -> None:
        task = loop.create_task(_awaited(make), context=context)

        def done(finished: asyncio.Task[T]) -> None:
            if finished.cancelled():
                result.cancel()
            elif (error := finished.exception()) is not None:
                result.set_exception(error)
            else:
                result.set_result(finished.result())

        task.add_done_callback(done)

    loop.call_soon_threadsafe(start)
    return result.result()


async def _awaited(make: Callable[[], Awaitable[T]]) -> T:
    return await make()
