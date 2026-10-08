"""Subagent cancellation admission and owner-settlement boundary."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from clio_agent.gact.task_projection import TERMINAL, task_views

logger = logging.getLogger(__name__)


def close_subtree_admission(app: Any, child_sid: str) -> None:
    """Persist closure for the whole existing subtree before any cancellation request."""
    closed = getattr(app.state, "task_admission_closed", set())
    app.state.task_admission_closed = closed
    from clio_agent.gact.session_descendants import descendant_session_ids

    for sid in [child_sid, *descendant_session_ids(app, child_sid)]:
        closed.add(sid)
        if app.state.sessions.get(sid) is not None:
            app.state.sessions.update(sid, metadata_patch={"task_admission_closed": True})


async def _cancel_owned(app: Any, row: dict[str, Any]) -> None:
    """Retain honest pending state if a descendant owner cannot acknowledge cancellation."""
    from clio_agent.gact.task_supervisor import task_supervisor

    try:
        await asyncio.to_thread(task_supervisor(app).request_cancel, row)
    except (RuntimeError, ValueError):
        logger.exception("Descendant cancellation failed handle=%s", row["handle"])


def request_subagent_cancel(app: Any, task: Any) -> Any:
    """Close admission and signal the child without publishing premature terminal state."""
    from clio_agent.gact.agent_tasks import persist_agent_task, publish_agent_task_event
    from clio_agent.providers.claude_code_cancel import abort_session_streams

    child_sid = task.child_session_id
    close_subtree_admission(app, child_sid)
    if task.cancel_requested:
        return task
    updated = replace(task, cancel_requested=True)
    persist_agent_task(app, updated)
    publish_agent_task_event(app, updated, "agent.task.updated")
    app.state.cancel_flags.add(child_sid)
    event = app.state.cancel_events.get(child_sid)
    if event is not None:
        event.set()
    abort_session_streams(child_sid)
    for row in task_views(app, child_sid):
        if row["task_kind"] != "Subagent" and row["effective_status"] not in TERMINAL:
            asyncio.create_task(_cancel_owned(app, row))
    # A queued/standing child has no worker; settle only after descendant owners settle.
    if not task.is_terminal and not app.state.turn_runner.busy(child_sid):
        asyncio.create_task(_settle_when_children_finish(app, task.task_id, child_sid))
    return updated


async def _settle_when_children_finish(app: Any, task_id: str, child_sid: str) -> None:
    from clio_agent.gact.turn_spawn import _on_child_done

    while any(r["effective_status"] not in TERMINAL for r in task_views(app, child_sid)):
        await asyncio.sleep(0.1)
    _on_child_done(app, task_id, child_sid, "async")


def defer_subagent_settlement(app: Any, task: Any) -> bool:
    """Delay cancelled-child completion until every descendant owner has settled."""
    if not task.cancel_requested:
        return False
    if any(r["effective_status"] not in TERMINAL for r in task_views(app, task.child_session_id)):
        asyncio.create_task(_settle_when_children_finish(app, task.task_id, task.child_session_id))
        return True
    return False
