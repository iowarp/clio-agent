"""Application supervision over the existing relay subagent execution owner."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from clio_agent.gact.agent_tasks import persist_agent_task, publish_agent_task_event
from clio_agent.gact.agents.invoker import TaskHandle
from clio_agent.gact.agents.spawn_placement import invoker_for_task
from clio_agent.gact.task_projection import TERMINAL, task_views
from clio_agent.gact.task_supervisor import task_supervisor

logger = logging.getLogger(__name__)


def retain_relay_owner(app: Any, task: Any) -> None:
    """Resume the retained identity through its owner without submitting another job."""
    from clio_agent.gact.loop_handoff import call_on_loop

    if not task.placement.startswith("relay:") or task.is_terminal:
        return
    cluster = task.placement.split(":", 1)[1]
    if cluster not in getattr(app.state, "relay_expert_invokers", {}):
        _connection(app, task, "disconnected")
        return
    supervisor = task_supervisor(app)

    def start() -> None:
        if task.task_id not in supervisor.drivers:
            supervisor.supervise(task.task_id, _drive(app, task))

    call_on_loop(app.state.mcp_app_loop, start, op="retain relay task owner")


def _wait_owner(app: Any, task: Any) -> Any:
    """Use the existing exclusive driver lease within a bounded observation window."""
    from clio_agent.gact.runtime.globals import _gact_app_context

    with _gact_app_context(app):
        return invoker_for_task(app, task).invoker.wait(TaskHandle.from_task(task), timeout_s=1.0)


async def _drive(app: Any, task: Any) -> None:
    """Keep observing accepted relay work on application lifetime, independent of turns."""
    while not task_supervisor(app).closing:
        current = app.state.agent_task_registry.get(task.task_id)
        if current is None or current.is_terminal:
            return
        try:
            await asyncio.to_thread(_wait_owner, app, current)
        except asyncio.CancelledError:
            _connection(app, current, "disconnected")
            raise
        except Exception:
            if _connection(app, current, "disconnected"):
                logger.exception("Relay observation disconnected task=%s", task.task_id)
        else:
            _connection(app, current, "connected")
        await asyncio.sleep(1.0)


def _connection(app: Any, task: Any, freshness: str) -> bool:
    """Keep connection state separate from the authoritative remote task status."""
    from clio_agent.gact.mcp_task_store import app_task_store

    store = app_task_store(app)
    from clio_agent.gact.task_backend_identity import relay_key

    key = relay_key(app, task)
    record = store.get(key) if key is not None else None
    records = [record] if record is not None else []
    if len(records) != 1 or records[0].connection_freshness == freshness:
        return False
    store.put(replace(records[0], connection_freshness=freshness))
    return True


def request_relay_cancel(app: Any, task: Any) -> bool:
    """Acknowledge the actual remote owner and close admission before subtree cancellation."""
    from clio_agent.gact.loop_handoff import call_on_loop
    from clio_agent.gact.task_subagent_owner import _cancel_owned, close_subtree_admission

    if task.is_terminal:
        return False

    def begin() -> Any:
        close_subtree_admission(app, task.child_session_id)
        current = app.state.agent_task_registry.get(task.task_id)
        if not current.cancel_requested:
            current = replace(current, cancel_requested=True)
            persist_agent_task(app, current)
            publish_agent_task_event(app, current, "agent.task.updated")
            for row in task_views(app, task.child_session_id):
                if row["effective_status"] not in TERMINAL:
                    asyncio.create_task(_cancel_owned(app, row))
        return current

    current = call_on_loop(app.state.mcp_app_loop, begin, op="close relay task admission")
    supervisor = task_supervisor(app)

    async def request() -> bool:
        return await asyncio.to_thread(_cancel_owner, app, current)

    with supervisor.delivery_lock:
        future = supervisor.cancellation_requests.get(task.task_id)
        if future is None:
            future = asyncio.run_coroutine_threadsafe(request(), app.state.mcp_app_loop)
            supervisor.cancellation_requests[task.task_id] = future
    try:
        acknowledged = future.result(timeout=30)
    except Exception as exc:
        if future.done():
            with supervisor.delivery_lock:
                supervisor.cancellation_requests.pop(task.task_id, None)
        raise RuntimeError(f"Relay cancellation was not acknowledged: {exc}") from exc
    if not acknowledged:
        if app.state.agent_task_registry.get(task.task_id).is_terminal:
            return False
        raise RuntimeError("Relay owner did not acknowledge cancellation")
    retain_relay_owner(app, current)
    return True


def _cancel_owner(app: Any, task: Any) -> bool:
    """Route cancellation through the same configured placement that accepted the job."""
    from clio_agent.gact.runtime.globals import _gact_app_context

    with _gact_app_context(app):
        return bool(invoker_for_task(app, task).invoker.cancel(TaskHandle.from_task(task)))
