"""Run a synchronous call on the app event loop from another thread, typed on failure.

The spawn and spotter hand-offs (``turn_spawn.spawn_child_turn_threadsafe``,
``spotter_watcher._start_check_turn_on_app_loop``) used
``run_coroutine_threadsafe(...).result(timeout=60/30)``: a loop that was stopped made the
caller wait out the bound and then raise a bare ``TimeoutError`` -- and the call could
still run afterwards, behind the caller's back (#1577 3.9).

:func:`call_on_loop` instead:

* fails at once with :class:`LoopHandoffError` (:data:`REASON_LOOP_NOT_RUNNING`) when the
  loop is closed or not running -- nothing will ever process the call;
* waits while the loop is alive and processing (a heartbeat posted each
  :data:`HANDOFF_SLICE_S` runs), however busy it is;
* fails typed (:data:`REASON_LOOP_STALLED`) when the loop has processed nothing for
  :data:`STALL_CEILING_S`, and the call is then abandoned: it never runs later;
* once the call has started on the loop, waits for it to finish (it is the loop's own
  work), logging while it runs, typed (:data:`REASON_CALL_OVERRAN`) only past the ceiling.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: How often the caller checks on the loop while waiting.
HANDOFF_SLICE_S = 5.0
#: How long the loop may process nothing (or the started call may run) before a typed failure.
STALL_CEILING_S = 180.0

REASON_LOOP_NOT_RUNNING = "loop_not_running"
REASON_LOOP_STALLED = "loop_stalled"
REASON_CALL_OVERRAN = "call_overran"


class LoopHandoffError(RuntimeError):
    """A call handed to the app loop could not run there; ``reason`` is typed."""

    def __init__(self, op: str, reason: str, detail: str) -> None:
        self.op = op
        self.reason = reason
        super().__init__(f"{op}: {reason} ({detail})")


def call_on_loop(loop: asyncio.AbstractEventLoop, fn: Callable[[], T], *, op: str) -> T:
    """Run ``fn()`` on ``loop`` (from another thread) and return its result.

    Raises:
        LoopHandoffError: the loop is not running, stalled for the ceiling (the call is
            abandoned and never runs), or the started call overran the ceiling.
        Exception: whatever ``fn`` itself raised.
    """
    if loop.is_closed() or not loop.is_running():
        raise _fail(op, REASON_LOOP_NOT_RUNNING, "the app event loop is not running")
    gate = threading.Lock()
    state = {"started": False, "abandoned": False}

    async def _call() -> T:
        with gate:
            if state["abandoned"]:
                raise asyncio.CancelledError
            state["started"] = True
        return fn()

    try:
        future = asyncio.run_coroutine_threadsafe(_call(), loop)
    except RuntimeError as exc:  # closed between the check and the submit
        raise _fail(op, REASON_LOOP_NOT_RUNNING, str(exc)) from exc
    quiet_since = time.monotonic()
    beat = _post_beat(loop)
    while True:
        try:
            return future.result(timeout=HANDOFF_SLICE_S)
        except concurrent.futures.TimeoutError:
            pass
        now = time.monotonic()
        if beat is None or loop.is_closed() or not loop.is_running():
            _abandon(gate, state)
            raise _fail(op, REASON_LOOP_NOT_RUNNING, "the app event loop stopped")
        if beat.is_set():
            quiet_since = now
            beat = _post_beat(loop)
        waited = now - quiet_since
        if state["started"]:
            if waited >= STALL_CEILING_S:
                raise _fail(
                    op, REASON_CALL_OVERRAN, f"still running on the loop after {waited:.0f}s"
                )
            logger.info("loop hand-off running reason=call_running op=%s", op)
            continue
        if waited >= STALL_CEILING_S and _abandon(gate, state):
            raise _fail(op, REASON_LOOP_STALLED, f"the loop processed nothing for {waited:.0f}s")
        logger.info("loop hand-off waiting reason=loop_busy op=%s quiet_s=%.0f", op, waited)


def _post_beat(loop: asyncio.AbstractEventLoop) -> threading.Event | None:
    """Post a heartbeat the loop sets when it next runs callbacks; ``None`` if it is closed."""
    beat = threading.Event()
    try:
        loop.call_soon_threadsafe(beat.set)
    except RuntimeError:
        return None
    return beat


def _abandon(gate: threading.Lock, state: dict[str, bool]) -> bool:
    """Mark the call abandoned unless it already started; ``True`` when abandoned."""
    with gate:
        if state["started"]:
            return False
        state["abandoned"] = True
        return True


def _fail(op: str, reason: str, detail: str) -> LoopHandoffError:
    logger.warning("loop hand-off failed reason=%s op=%s detail=%s", reason, op, detail)
    return LoopHandoffError(op, reason, detail)


__all__ = [
    "HANDOFF_SLICE_S",
    "LoopHandoffError",
    "REASON_CALL_OVERRAN",
    "REASON_LOOP_NOT_RUNNING",
    "REASON_LOOP_STALLED",
    "STALL_CEILING_S",
    "call_on_loop",
]
