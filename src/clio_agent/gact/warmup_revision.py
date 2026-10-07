"""Keep background fleet preparation within the app's runtime revision boundary."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from clio_agent.gact.turn_revision_gate import TurnRevisionGate


async def _finish_thread(work: Callable[[], None]) -> None:
    """Retain the revision reader until its worker really finishes, even on cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(work))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        await asyncio.shield(worker)
        raise


def start_revision_warmup(
    app: Any,
    work: Callable[[], None],
    *,
    name: str,
    finished: Callable[[], None],
) -> threading.Thread | asyncio.Task[None]:
    """Start preparation off-loop, draining or queuing with the app's revision gate.

    Warm-ups created by a running turn retain its descendant context, so a
    waiting writer cannot deadlock that turn. Synchronous standalone callers
    without a running app loop retain the existing thread interface.
    """
    state = getattr(app, "state", None)
    gate = getattr(getattr(state, "turn_runner", None), "revision_gate", None)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and isinstance(gate, TurnRevisionGate):
        task = loop.create_task(gate.run(_finish_thread(work)), name=name)
        task.add_done_callback(lambda _task: finished())
        return task

    def run() -> None:
        try:
            work()
        finally:
            finished()

    thread = threading.Thread(target=run, name=name, daemon=True)
    try:
        thread.start()
    except RuntimeError:
        finished()
        raise
    return thread
