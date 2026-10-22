"""Run an owner's coroutine method on the event loop the owner is bound to."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

_Result = TypeVar("_Result")


def on_owner_loop(
    method: Callable[..., Coroutine[Any, Any, _Result]],
) -> Callable[..., Awaitable[_Result]]:
    """Run ``method`` on ``self._loop`` when it is called from another loop.

    A model's tool call can reach an owner from a short-lived loop (an executor
    thread's ``asyncio.run``). Tasks and futures created there die when that loop
    closes, and the owner's own tasks cannot be awaited from it, so the call hops
    to the owner's loop and waits there.
    """

    @functools.wraps(method)
    async def run(self: Any, *args: Any, **kwargs: Any) -> _Result:
        loop = self._loop
        if loop is None or loop is asyncio.get_running_loop():
            return await method(self, *args, **kwargs)
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(method(self, *args, **kwargs), loop)
        )

    return run


__all__ = ["on_owner_loop"]
