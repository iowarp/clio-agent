"""Who cancelled a turn: the client, or the server's shutdown drain.

A turn the shutdown drain stops is the server's cancellation, not the client's:
it carries ``reason=server_shutdown`` and says so. This is a gact leaf (it imports
only ``gact.context`` and ``gact.types``); ``gact.runtime.globals`` re-exports it.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.types import ErrorInfo

_CLIENT_MESSAGE = "turn cancelled by client"
_SHUTDOWN_MESSAGE = "turn cancelled by server shutdown"


def turn_cancelled_by_shutdown(app: Any | None = None) -> bool:
    """Whether the server's shutdown drain (not a client) is cancelling turns.

    ``app`` defaults to the active GACT app of the calling context.
    """
    if app is None:
        app = _ctx.active_app()
    runner = getattr(getattr(app, "state", None), "turn_runner", None)
    return getattr(runner, "closing", False) is True


def cancellation_outcome(
    details: dict[str, Any], *, app: Any | None = None
) -> tuple[str, dict[str, Any]]:
    """The cancellation message and ``details`` (plus ``reason`` on shutdown)."""
    if not turn_cancelled_by_shutdown(app):
        return _CLIENT_MESSAGE, details
    from clio_agent.gact.turn_runner import DRAIN_REASON_SERVER_SHUTDOWN  # noqa: PLC0415

    return _SHUTDOWN_MESSAGE, {**details, "reason": DRAIN_REASON_SERVER_SHUTDOWN}


def _cancelled_error_info(
    sid: str, *, execution_cancellation: str, app: Any | None = None
) -> ErrorInfo:
    """ErrorInfo for a cancelled turn (the old, always-redundant
    ``executor_work_may_continue`` boolean is deleted, L1 slice)."""
    message, details = cancellation_outcome(
        {"session_id": sid, "execution_cancellation": execution_cancellation}, app=app
    )
    return ErrorInfo(error="cancelled", message=message, details=details, recoverable=True)
