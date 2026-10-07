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
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    worker.result()
    if cancelled:
        raise asyncio.CancelledError


def start_revision_warmup(
    app: Any,
    work: Callable[[], None],
    *,
    name: str,
    aborted: Callable[[], None],
) -> threading.Thread | asyncio.Task[None]:
    """Start preparation off-loop, draining or queuing with the app's revision gate.

    Warm-ups created by a running turn retain its descendant context, so a
    waiting writer cannot deadlock that turn. Synchronous standalone callers
    without a running app loop retain the existing thread interface.
    The worker owns normal cleanup; ``aborted`` releases preparation that never
    starts, including cancellation while queued behind a revision.
    """
    started = threading.Event()

    def marked_work() -> None:
        started.set()
        work()

    def abort_if_not_started() -> None:
        if not started.is_set():
            aborted()

    state = getattr(app, "state", None)
    gate = getattr(getattr(state, "turn_runner", None), "revision_gate", None)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and isinstance(gate, TurnRevisionGate):

        async def run_revision() -> None:
            await gate.run(_finish_thread(marked_work))

        task = loop.create_task(run_revision(), name=name)
        task.add_done_callback(lambda _task: abort_if_not_started())
        return task

    thread = threading.Thread(target=marked_work, name=name, daemon=True)
    try:
        thread.start()
    except RuntimeError:
        abort_if_not_started()
        raise
    return thread
