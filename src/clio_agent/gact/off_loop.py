"""Off-loop helpers for blocking store work reached from coroutines (#1334).

The server loop thread must never wait on an ARC store RPC (``arc.loop_guard`` makes
that a typed defect). Three shapes cover every caller:

* :func:`run_off_loop` — a coroutine awaits a blocking callable on an executor with the
  caller's contextvars copied (the ``turn_forward._run_turn_setup_off_loop`` pattern,
  usable without a ``TurnState``). The loop stays free; the caller still gets the
  result / exception, so must-succeed contracts are unchanged.
* :func:`emit_semantic_event_async` — the awaited twin of
  ``runtime.globals._emit_semantic_event`` for ``async def`` routes.
* :func:`schedule_off_loop` — for SYNC code that may be invoked from a coroutine
  without an ``await`` seam (the session lifecycle observer inside ``SessionStore``):
  when the calling thread runs a loop the work is dispatched to an executor and the
  future returned; failures are audited (``off_loop.job_failed``) and logged, never
  dropped. Without a running loop the callable runs inline.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
from typing import Any, Callable, Optional, TypeVar

from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

T = TypeVar("T")

OFF_LOOP_JOB_FAILED = "off_loop_job_failed"


async def run_off_loop(
    fn: Callable[..., T],
    *args: Any,
    executor: Optional[concurrent.futures.Executor] = None,
) -> T:
    """Await ``fn(*args)`` on ``executor`` (default pool) with contextvars copied."""

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(executor, lambda: ctx.run(fn, *args))


async def emit_semantic_event_async(
    app: Any, sid: str, event_type: str, **kwargs: Any
) -> dict[str, Any]:
    """Emit one semantic event from a coroutine without holding the loop for its RPC."""

    from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

    return await run_off_loop(lambda: _emit_semantic_event(app, sid, event_type, **kwargs))


def _audited(label: str, fn: Callable[[], Any]) -> Callable[[], Any]:
    def _run() -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - surfaced typed below, never dropped
            stream_audit(
                "off_loop.job_failed",
                label=label,
                reason=OFF_LOOP_JOB_FAILED,
                error=type(exc).__name__,
                message=str(exc)[:300],
            )
            logger.error("off-loop job %s failed (%s)", label, OFF_LOOP_JOB_FAILED, exc_info=True)
            raise

    return _run


def schedule_off_loop(fn: Callable[[], Any], *, label: str) -> Optional["asyncio.Future[Any]"]:
    """Run ``fn`` inline when no loop runs on this thread, else dispatch it off the loop.

    Returns the future when dispatched (so a caller that CAN await may), ``None`` when
    the callable ran inline. A dispatched failure is audited + logged with a typed
    reason; it is never silently dropped.
    """

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        fn()
        return None
    ctx = contextvars.copy_context()
    return loop.run_in_executor(None, lambda: ctx.run(_audited(label, fn)))


__all__ = [
    "OFF_LOOP_JOB_FAILED",
    "emit_semantic_event_async",
    "run_off_loop",
    "schedule_off_loop",
]
