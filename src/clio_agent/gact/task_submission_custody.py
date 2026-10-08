"""Track in-flight acceptance on the existing application supervisor, without replay."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any


def begin_submission(app: Any, sid: str) -> Future[None]:
    """Claim admission before dispatch so subtree settlement includes uncertain acceptance."""
    from clio_agent.gact.task_supervisor import task_supervisor
    from clio_agent.tools.task_call_context import require_admission

    supervisor = task_supervisor(app)
    ticket: Future[None] = Future()
    with supervisor.delivery_lock:
        require_admission(app, sid)
        supervisor.submission_owners[ticket] = sid
    return ticket


def finish_submission(app: Any, ticket: Future[None]) -> None:
    """Release custody only after the accepted record and its owner exist, or dispatch fails."""
    from clio_agent.gact.task_supervisor import task_supervisor

    supervisor = task_supervisor(app)
    with supervisor.delivery_lock:
        supervisor.submission_owners.pop(ticket, None)
        ticket.set_result(None)


@contextmanager
def submission_scope(app: Any, sid: str) -> Iterator[None]:
    """Retain a synchronous owner's single submission through acceptance or failure."""
    ticket = begin_submission(app, sid)
    try:
        yield
    finally:
        finish_submission(app, ticket)


def pending_submissions(app: Any, root_sid: str) -> bool:
    """Check existing submission futures within a cancelling subtree before terminal publication."""
    from clio_agent.gact.session_descendants import descendant_session_ids
    from clio_agent.gact.task_supervisor import task_supervisor

    supervisor = task_supervisor(app)
    scope = {root_sid, *descendant_session_ids(app, root_sid)}
    with supervisor.delivery_lock:
        return any(sid in scope for sid in supervisor.submission_owners.values())


def cancel_accepted_if_closed(app: Any, sid: str, handle: str) -> None:
    """Join late acceptance to explicit subtree cancellation after durable owner installation."""
    from clio_agent.gact.loop_handoff import call_on_loop
    from clio_agent.gact.task_projection import resolve_task
    from clio_agent.gact.task_subagent_owner import _cancel_owned
    from clio_agent.tools.task_call_context import require_admission

    def reconcile() -> None:
        try:
            require_admission(app, sid)
        except RuntimeError:
            # This schedules the same owning cancellation operation as the public
            # control. The accepted record is discoverable even if its waiter stopped.
            asyncio.create_task(_cancel_owned(app, resolve_task(app, sid, handle)))

    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is app.state.mcp_app_loop:
        reconcile()
    else:
        call_on_loop(app.state.mcp_app_loop, reconcile, op="reconcile late task acceptance")
