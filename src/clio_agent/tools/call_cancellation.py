"""Synchronous waits inside a tool call that end when the person stops the turn.

A tool call can block on work it does not own outright: a server listing that waits on
a uv lock, a provision another call started, a connect on the executor loop. Only the
person stops a run, and Stop must settle promptly, so these waits poll the turn's
cancellation and raise :class:`~clio_agent.errors.CancellationError` when it is set.

Shared work is left behind (a provision keeps building for whoever needs it next);
work the call owns alone (its own listing) is stopped with it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from typing import Any

from clio_agent.errors import CancellationError

_POLL_S = 0.05
#: How long a stopped call lets its own work close (client, spawned server) before it
#: returns anyway.
_SETTLE_S = 3.0


def call_cancelled() -> bool:
    """Whether the turn this call belongs to was stopped (or its scope was).

    Reads the cooperative checker bound on this context and the live turn's
    ``cancellation_checker`` hook; a checker that fails reads as not stopped.
    """

    from clio_agent.runtime.cancellation import cancellation_requested  # noqa: PLC0415
    from clio_agent.tools.execution import resolved_tool_runtime  # noqa: PLC0415 - cycle

    try:
        if cancellation_requested():
            return True
        hooks = resolved_tool_runtime()
        checker = hooks.cancellation_checker if hooks is not None else None
        return bool(checker is not None and checker())
    except Exception:  # noqa: BLE001 - a broken checker never ends a wait
        return False


def cancelled_error(stage: str, *, left_behind: bool = False) -> CancellationError:
    """The error a stopped wait raises; ``left_behind`` names shared work that goes on."""

    from clio_agent.tools.foreground_cancellation import (  # noqa: PLC0415
        SERVER_SHUTDOWN,
        cancellation_cause,
    )

    details: dict[str, Any] = {"stage": stage, "execution_cancellation": "cooperative"}
    if left_behind:
        details["left_behind"] = True
    if cancellation_cause() == SERVER_SHUTDOWN:
        details["cause"] = SERVER_SHUTDOWN
        return CancellationError(
            "tool call stopped because the agent service is shutting down", details=details
        )
    return CancellationError("tool call cancelled by client", details=details)


def wait_future(
    future: concurrent.futures.Future[Any],
    timeout: float | None = None,
    *,
    stage: str,
    cancel_work: bool = False,
) -> Any:
    """``future.result(timeout)``, raising ``CancellationError`` once the turn is stopped.

    ``cancel_work`` also cancels ``future`` (work this call owns); otherwise it is left
    to finish on its own.
    """

    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            return future.result(timeout=_POLL_S if remaining is None else min(_POLL_S, remaining))
        except concurrent.futures.TimeoutError:
            if future.done():
                return future.result()
            if call_cancelled():
                if cancel_work:
                    future.cancel()
                raise cancelled_error(stage, left_behind=not cancel_work) from None
            if deadline is not None and time.monotonic() >= deadline:
                raise


def wait_event(event: threading.Event, timeout: float | None = None, *, stage: str) -> bool:
    """``event.wait(timeout)``, raising ``CancellationError`` once the turn is stopped.

    What ``event`` waits on is left behind.
    """

    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if event.wait(_POLL_S if remaining is None else min(_POLL_S, remaining)):
            return True
        if call_cancelled():
            raise cancelled_error(stage, left_behind=True)
        if deadline is not None and time.monotonic() >= deadline:
            return False


async def until_cancelled(awaitable: Any, *, stage: str) -> Any:
    """Await ``awaitable`` (work this call owns) and stop it once the turn is stopped.

    The awaited task is cancelled and given :data:`_SETTLE_S` to close what it opened,
    then ``CancellationError`` is raised. Run it in the calling tool's context (a fresh
    loop on that thread copies it) so the turn's stop is visible.
    """

    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=_POLL_S)
            if done:
                return task.result()
            if call_cancelled():
                task.cancel()
                await asyncio.wait({task}, timeout=_SETTLE_S)
                raise cancelled_error(stage)
    finally:
        if not task.done():
            task.cancel()


__all__ = [
    "call_cancelled",
    "cancelled_error",
    "until_cancelled",
    "wait_event",
    "wait_future",
]
