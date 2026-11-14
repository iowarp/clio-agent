"""Cooperative stop for a running flow.

A flow stops when its assignment receives a cancellation request: the person's Stop
with ``children: stop``, ``cancel_tasks`` on the flow, or the cancellation of an
ancestor. Admission closes at once, so ``ctx.call``/``ctx.start``/``ctx.gather``
refuse new work with :class:`CallCancelled` (``flows.dispatch``). A step that makes
no task (``ctx.step``) checks here first, and a flow can read
``await ctx.stop_requested()`` between phases. The journal keeps every finished
call, so a restart resumes from them (``docs/design/stop-and-restart.md``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from clio_agent.flows.outcomes import CallCancelled


def _requested(app: Any, handle: str) -> bool:
    """Admission's own answer: closed by a stop on this assignment or an ancestor."""

    from clio_agent.gact.task_access import AdmissionClosed  # noqa: PLC0415

    task = app.state.task_history.task_snapshot(handle)
    if task is None:
        return False
    if task.cancellation_requested_at is not None or task.status == "cancelled":
        return True
    try:
        app.state.conversations.require_admission(task)
    except AdmissionClosed as closed:
        return bool(closed.stopping)
    except ValueError:
        return False  # another closed reason (a lineage error) is not a stop
    return False


async def stop_requested(app: Any, root_handle: str) -> bool:
    """Whether the run's assignment ``root_handle`` (or an ancestor) was asked to stop."""

    state = getattr(app, "state", None)
    history = getattr(state, "task_history", None)
    if history is None or getattr(state, "conversations", None) is None or not root_handle:
        return False
    return bool(await history.io.run(_requested, app, root_handle))


async def raise_if_stopped(app: Any, root_handle: str, name: str) -> None:
    """Refuse to start step ``name`` once the run was asked to stop."""

    if await stop_requested(app, root_handle):
        raise CallCancelled(
            f"the run was stopped before step {name!r}", target=name, status="cancelled"
        )


def _origin(app: Any, handle: str) -> dict[str, Any]:
    """Where the stop that reached ``handle`` started, walking up its owners.

    The first assignment on the way up whose cancellation was asked for by a person or
    an agent, rather than passed down from its parent, is the origin; its recorded
    ``cancellation_by`` and ``cancellation_reason`` say who asked and why.
    """

    from clio_agent.arc.task_types import AssignmentDetails  # noqa: PLC0415

    history = app.state.task_history
    seen: set[str] = set()
    current = history.task_snapshot(handle)
    while current is not None and current.handle not in seen:
        seen.add(current.handle)
        if current.cancellation_requested_at is not None and current.details is not None:
            details = AssignmentDetails.model_validate(history.read_content(current.details))
            if details.cancellation_by not in {None, "parent"}:
                return {
                    "stopped_by": details.cancellation_by,
                    **(
                        {"stop_reason": details.cancellation_reason}
                        if details.cancellation_reason
                        else {}
                    ),
                    "stopped_handle": current.handle,
                }
        parent = current.owner.parent_handle
        current = history.task_snapshot(parent) if parent else None
    return {"stopped_by": "unknown"}


async def stopped_ending(
    app: Any, root_handle: str, ending: tuple[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """How a run that raised ended, when its assignment was asked to stop.

    A stop shows up inside the flow as whatever its code made of the cancelled calls:
    a council's ``gather_failed``, or ``call_cancelled`` naming the child whose call
    was cancelled. The recorded fact is the stop, so the run ends ``cancelled`` with
    reason ``stopped``, who asked (``stopped_by``: ``person``, ``agent``, ``unknown``)
    and why (``stop_reason``, such as ``conversation_stop``). The outcome its code saw
    stays as ``ended_by``, never as the reason, so a child is not blamed for the stop.
    """

    status, result = ending
    if not await stop_requested(app, root_handle):
        return ending
    origin = await _recorded_origin(app, root_handle)
    kept = {key: result[key] for key in ("handle", "step") if result.get(key)}
    return "cancelled", {
        "reason": "stopped",
        **origin,
        **kept,
        "message": _message(origin),
        "ended_by": {"status": status, **result},
    }


#: Admission closes the moment a stop is accepted; the request that names who asked is
#: recorded just after. A run that ends in between waits this long for it.
ORIGIN_WAIT_S = 5.0


async def _recorded_origin(app: Any, root_handle: str) -> dict[str, Any]:
    """The stop's origin once its request is recorded, or ``unknown`` after the wait."""

    io = app.state.task_history.io
    deadline = asyncio.get_running_loop().time() + ORIGIN_WAIT_S
    while True:
        origin = await io.run(_origin, app, root_handle)
        if origin["stopped_by"] != "unknown" or asyncio.get_running_loop().time() >= deadline:
            return origin
        await asyncio.sleep(0.05)


def _message(origin: dict[str, Any]) -> str:
    """The run's one-line reason, for readers without the typed fields."""

    if origin.get("stop_reason") == "conversation_stop":
        return "Stopped from the conversation's Stop."
    return {
        "person": "Stopped on a person's request.",
        "agent": "Stopped by an agent's cancellation.",
    }.get(str(origin.get("stopped_by")), "Stopped on request.")


__all__ = ["raise_if_stopped", "stop_requested", "stopped_ending"]
