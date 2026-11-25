"""Name the code a stuck call is waiting in, without changing what it waits for.

:func:`watch` traces the watched thread's stack once when its block has not finished
within ``seconds``: a call that stalls before its ``tool.call.started`` event (after a
live MCP configuration change, 2026-11-25) then shows where it stopped instead of only
that nothing more happened.
"""

from __future__ import annotations

import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager

from clio_agent.runtime import trace

#: Typed trace event: ``call_stage_stalled stage=<stage> name=<tool> waited_s=<s> stack=...``.
CALL_STAGE_STALLED = "call_stage_stalled"
STALL_REPORT_SECONDS = 15.0
_local = threading.local()


def _report(thread_id: int, stage: str, name: str, seconds: float) -> None:
    frame = sys._current_frames().get(thread_id)  # noqa: SLF001 - diagnostic read only
    if frame is None:
        return
    stack = "".join(traceback.format_stack(frame)[-14:]).replace("\n", " | ")
    trace.event(
        "TOOLS",
        "%s stage=%s name=%s waited_s=%.0f stack=%s",
        CALL_STAGE_STALLED,
        stage,
        name,
        seconds,
        stack,
    )


@contextmanager
def watch(stage: str, name: str = "", seconds: float = STALL_REPORT_SECONDS) -> Iterator[None]:
    """Run the block; trace this thread's stack once if it is still inside after ``seconds``
    (or, sooner, until the block calls :func:`passed`)."""

    timer = threading.Timer(seconds, _report, args=(threading.get_ident(), stage, name, seconds))
    timer.daemon = True
    outer = getattr(_local, "timer", None)
    _local.timer = timer
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        _local.timer = outer


def passed() -> None:
    """The watched stage of this thread's innermost :func:`watch` is over."""

    timer = getattr(_local, "timer", None)
    if timer is not None:
        timer.cancel()


__all__ = ["CALL_STAGE_STALLED", "STALL_REPORT_SECONDS", "passed", "watch"]
