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


async def stopped_ending(
    app: Any, root_handle: str, ending: tuple[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """How a run that raised ended, when its assignment was asked to stop.

    A stop shows up inside the flow as whatever its code made of the cancelled calls
    (a council's ``gather_failed``, a call's ``call_failed``). The recorded fact is the
    stop, so the run ends ``cancelled`` and keeps the outcome its code saw as
    ``ended_by``.
    """

    status, result = ending
    if status == "cancelled" or not await stop_requested(app, root_handle):
        return ending
    return "cancelled", {**result, "reason": "stopped", "ended_by": status}


__all__ = ["raise_if_stopped", "stop_requested", "stopped_ending"]
