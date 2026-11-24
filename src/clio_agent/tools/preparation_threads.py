"""The threads a server's own preparation runs its short blocking steps on.

Callers that wait for a server's preparation (a page read, a turn's first call) block a
thread of the loop's default executor until it is done. The preparation's own blocking
steps (taking the launcher cache lock, sampling the server's process tree) must not
queue on that same pool: a burst of waiters fills it, and the one preparation they all
wait for can then never take its next step. Every step run here is short and never waits
on a preparation, so this pool always drains.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="clio-prepare-io")


async def run_preparation_step(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Await ``fn(*args, **kwargs)`` on the preparation pool, in the caller's context."""

    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    return await loop.run_in_executor(_POOL, functools.partial(context.run, fn, *args, **kwargs))


__all__ = ["run_preparation_step"]
