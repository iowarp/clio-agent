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
    """Whether the server's shutdown (not a client) is cancelling turns.

    True once the turn drain is closing, and also from the moment the server was asked
    to exit (``uvicorn``'s ``should_exit``: SIGTERM, SIGINT or a managed shutdown),
    which comes first. ``clio stop`` signals the server's whole process group, so a
    provider's command-line child is stopped by the same signal and its turn fails
    before the drain starts (CC-F3). ``app`` defaults to the active GACT app.
    """
    if app is None:
        app = _ctx.active_app()
    state = getattr(app, "state", None)
    runner = getattr(state, "turn_runner", None)
    if getattr(runner, "closing", False) is True:
        return True
    server = getattr(state, "uvicorn_server", None)
    return getattr(server, "should_exit", False) is True


def ended_by_shutdown(app: Any, outcome: Any) -> bool:
    """Whether a turn that ended with an error did so because the server is stopping.

    Such a segment is left open, as the shutdown drain leaves the turns it cancels, so
    the restart resumes its assignment in place (``conversation_recovery``). Settling
    it ``failed`` made a race decide between resumed and lost: a turn whose provider
    child died with the server's own SIGTERM before the drain settled first (CC-F4).
    """
    return getattr(outcome, "error", None) is not None and turn_cancelled_by_shutdown(app)


def shutdown_error_info(sid: str, exc: BaseException, *, app: Any | None = None) -> Any:
    """The cancellation a turn that failed while the server stops gets, or ``None``.

    A turn whose model or tool call failed because the server is shutting down was
    stopped by the shutdown, not by its provider: it reads as ``cancelled`` with
    ``reason=server_shutdown`` like a turn the drain cancels, and keeps the failure it
    saw as ``details.diagnostic``.
    """
    if not turn_cancelled_by_shutdown(app):
        return None
    info = _cancelled_error_info(sid, execution_cancellation="best_effort", app=app)
    info.details["diagnostic"] = f"{type(exc).__name__}: {exc}"[:500]
    return info


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
