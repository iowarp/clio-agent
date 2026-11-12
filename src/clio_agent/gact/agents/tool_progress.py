"""A running native tool's progress row, and the scope that keeps its document jobs stoppable."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

_ACTIVE_NATIVE_TOOL: ContextVar[tuple[str, Mapping[str, Any], Any] | None] = ContextVar(
    "clio_active_native_tool", default=None
)


def _elapsed(seconds: float) -> str:
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


def report_tool_progress(message: str, *, progress: float | None = None) -> bool:
    """Show ``message`` on the running native tool's row; ``False`` outside one."""

    active = _ACTIVE_NATIVE_TOOL.get()
    if active is None or not message:
        return False
    from clio_agent.tools import execution as _execution  # noqa: PLC0415

    name, args, handle = active
    try:
        _execution.notify_global_tool_observer(
            name,
            args,
            "progress",
            result={
                "observer_handle": handle,
                "progress": progress,
                "total": None,
                "message": message,
            },
        )
    except (RuntimeError, TimeoutError):
        # Delivery to the transcript failed (its loop is gone or busy); the tool runs on.
        logger.debug("reason=tool_progress_undelivered tool=%s", name, exc_info=True)
        return False
    return True


@contextlib.contextmanager
def _running_tool(name: str, args: Mapping[str, Any], handle: Any) -> Iterator[None]:
    """Scope one native call: progress reaches its row, and stopping the turn ends its jobs.

    Document jobs a tool starts (conversions, renders, builds) have no time limit;
    they run until they finish or the person stops the turn, which kills their
    process tree. While one runs, its row says so.
    """

    from clio_agent.runtime.cancellation import cancellation_requested  # noqa: PLC0415
    from clio_agent.runtime.document_stack.process import (  # noqa: PLC0415
        stall_message,
        watched,
    )

    def heartbeat(elapsed: float, stalled: float | None) -> None:
        if stalled is not None:
            report_tool_progress(
                f"Stalled after {_elapsed(elapsed)}: {stall_message('the document job', stalled)}",
                progress=round(elapsed, 1),
            )
            return
        report_tool_progress(
            f"Still running after {_elapsed(elapsed)}. Stop the turn to cancel.",
            progress=round(elapsed, 1),
        )

    token = _ACTIVE_NATIVE_TOOL.set((name, args, handle))
    try:
        with watched(cancellation_requested, heartbeat):
            yield
    finally:
        _ACTIVE_NATIVE_TOOL.reset(token)
