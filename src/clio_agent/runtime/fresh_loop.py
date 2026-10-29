"""Run a coroutine on its own event loop and leave nothing of that loop behind.

``asyncio.run`` is how synchronous code runs a coroutine: a tool call from the agent
loop, a flow's run, a probe. anyio keeps per-loop state in a ``WeakKeyDictionary``
keyed by the loop (``anyio.lowlevel.RunVar``). The first thread offload on a loop
(``anyio.to_thread.run_sync``, which tool and MCP code uses) caches the loop's root
task there, and the task refers to its loop, so the entry -- the loop, the task and
the task's context with every context variable it carried -- is never released. In
the agent process that context holds the running ReAct loop, its module, tools and
LM views, and the turn's LM ledger: each tool call kept a whole agent alive, and a
multi-agent run grew the process by gigabytes.

:func:`run_in_new_loop` is ``asyncio.run`` that drops the loop's anyio state when the
loop ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

__all__ = ["run_in_new_loop"]

T = TypeVar("T")


def run_in_new_loop(coroutine: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run(coroutine)``, releasing what anyio kept for the loop when it ends."""

    with asyncio.Runner() as runner:
        try:
            return runner.run(coroutine)
        finally:
            _release_anyio_state(runner.get_loop())


def _release_anyio_state(loop: asyncio.AbstractEventLoop) -> None:
    from anyio import lowlevel  # noqa: PLC0415 - anyio is imported only by the runs that use it

    run_vars = getattr(lowlevel, "_run_vars", None)
    if run_vars is not None:
        run_vars.pop(loop, None)
