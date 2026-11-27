"""Plain MCP calls: reachable by their task handle, recorded as running, cancellable.

A plain call is one the server answers directly (no MCP task was accepted for it), which is
how most tools run, a slow FreeRouting route included. Its record is written ahead as
``accepted`` before it is sent. Until now nothing held the call by its handle, so:

- the record stayed ``accepted`` while the call ran, and the Stop dialog showed a call that
  had run for minutes as "Queued";
- cancelling the handle found no call to cancel ("Original task transport is
  unavailable"), so "Stop everything" ended the reply while the tool ran to its end.

Here a sent call is recorded ``running``, and cancelling its handle cancels the call: the
MCP client then sends ``notifications/cancelled`` for the request, the server stops the work,
and the record ends ``cancelled`` with the typed reason and ``settlement: unknown`` (whether
the server acted on any of it before stopping is not known).
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from clio_agent.gact.native_work import now
from clio_agent.gact.view_call_ledger import settle_cut_off

if TYPE_CHECKING:
    from clio_agent.gact.external_tasks import ExternalTasks
    from clio_agent.gact.task_history import TaskHistory

logger = logging.getLogger(__name__)

#: Typed reason a plain call's record ends with when its cancel was requested.
PLAIN_CALL_CANCELLED = "mcp_call_cancelled"
PLAIN_CALL_CANCELLED_TEXT = (
    f"{PLAIN_CALL_CANCELLED}: the call was cancelled while the service was working on it; "
    "the service was told to stop, and how much of it the service did is not known."
)

#: The plain calls in flight, by task handle (handles are unique).
_IN_FLIGHT: dict[str, asyncio.Task[Any]] = {}


async def started(
    owner: ExternalTasks, handle: str, rpc: asyncio.Task[Any], *, written_ahead: bool
) -> None:
    """Hold a sent plain call by its handle; mark its written-ahead record running."""
    _IN_FLIGHT[handle] = rpc

    def forget(finished: asyncio.Task[Any]) -> None:
        if _IN_FLIGHT.get(handle) is finished:
            del _IN_FLIGHT[handle]

    rpc.add_done_callback(forget)
    if written_ahead:  # a read-only call is admitted with its outcome: nothing to update yet
        await owner._record_or_defer(
            "external-running:" + handle, partial(_mark_running, owner.history, handle)
        )


def _mark_running(history: TaskHistory, handle: str) -> None:
    with history.serialized():
        current = history.task_snapshot(handle)
        if current is None or current.terminal_at is not None or current.status != "accepted":
            return  # already answered, cancelled, or advanced by its own task protocol
        history.record_snapshot(
            current.model_copy(
                update={"status": "running", "activity": "executing", "updated_at": now()}
            ),
            observation_id=uuid4().hex,
        )


async def cancel(owner: ExternalTasks, handle: str) -> dict[str, Any] | None:
    """Cancel the plain call ``handle`` names; None when no such call is in flight."""
    rpc = _IN_FLIGHT.get(handle)
    if rpc is None or rpc.done():
        return None
    rpc.cancel()  # the MCP client sends notifications/cancelled for this request
    await owner._record_or_defer(
        "external-cancel-plain:" + handle,
        partial(settle_cut_off, owner.history, handle, PLAIN_CALL_CANCELLED_TEXT),
    )
    logger.info("%s handle=%s", PLAIN_CALL_CANCELLED, handle)
    return {
        "handle": handle,
        "cancellation_requested": True,
        "status": "cancelled",
        "settlement": "unknown",
        "recording": "unconfirmed"
        if any(
            owner.recovery.is_pending(prefix + handle)
            for prefix in ("external-cancel:", "external-cancel-plain:")
        )
        else "confirmed",
    }


def in_flight(handle: str) -> bool:
    """Whether a plain call with this handle is still running (tests, diagnostics)."""
    rpc = _IN_FLIGHT.get(handle)
    return rpc is not None and not rpc.done()
