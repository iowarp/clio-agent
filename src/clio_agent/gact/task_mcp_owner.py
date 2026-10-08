"""Application-owned MCP task driver, retaining the exact originating transport."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from clio_agent.gact.task_supervisor import task_supervisor
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecordStore

logger = logging.getLogger(__name__)


def accept_mcp_task(
    app: Any, session: Any, key: TaskKey, store: TaskRecordStore, executor: Any
) -> dict[str, Any]:
    """Detach the driver after durability and return the accepted public handle."""
    from clio_agent.tools.mcp_tasks import cancel_task

    row = store.get(key)
    if row is None or not row.handle or row.holding_reason:
        raise RuntimeError("Accepted MCP task lacks durable ownership")
    supervisor = task_supervisor(app)
    if executor is not None:
        supervisor.retained_executors.add(executor)
        executor._accepted_task_handles.add(row.handle)
    supervisor.supervise(
        row.handle,
        _drive(app, session, key, store, executor),
        lambda: cancel_task(session, key, store=store),
    )
    return {
        "accepted": True,
        "handle": row.handle,
        "kind": "MCP",
        "description": row.description,
        "status": "running",
        "backend_status": row.status,
        "controls": [
            "query_tasks",
            "observe_tasks",
            "wait_tasks",
            "cancel_tasks",
            "get_task_result",
        ],
    }


async def _drive(
    app: Any, session: Any, key: TaskKey, store: TaskRecordStore, executor: Any
) -> None:
    from clio_agent.gact.task_input_owner import owned_input_callback
    from clio_agent.tools.mcp_tasks import drive_task_to_terminal
    from clio_agent.tools.task_observers import resolve_task_observer
    from clio_agent.tools.task_receipt import (
        TaskResultValidationError,
        record_invalid_result,
        validate_terminal,
    )

    try:
        original = store.get(key)
        assert original is not None
        await drive_task_to_terminal(
            session,
            key,
            owned_input_callback(app, session, key, store),
            timeout_seconds=None,
            store=store,
            on_poll=resolve_task_observer(key),
            final_validator=lambda result: validate_terminal(original, result),
        )
    except TaskResultValidationError as exc:
        logger.exception("Task result violated its output contract identity=%s", key.row_key)
        record_invalid_result(store, key, exc)
    except asyncio.CancelledError:
        row = store.get(key)
        if row is not None and row.backend.get("transport") == "http":
            from clio_agent.gact.task_recovery import pause_http_observation

            pause_http_observation(store, key)
            raise
        if row is not None:
            store.put(
                replace(
                    row,
                    effective_status="interrupted",
                    connection_freshness="disconnected",
                    effective_status_reason="Application stopped observing this task",
                    result={
                        "error": "observation_interrupted",
                        "recoverable": row.backend.get("transport") == "http",
                    },
                    notify_pending=not bool(row.consumed_at),
                )
            )
        raise
    except Exception as exc:
        logger.exception("Task transport failed identity=%s", key.row_key)
        row = store.get(key)
        if row is not None:
            supervisor = task_supervisor(app)
            if row.backend.get("transport") == "http" and not supervisor.closing:
                from clio_agent.gact.task_recovery import recover_http

                supervisor.cancellers.pop(row.handle, None)
                store.put(
                    replace(
                        row, connection_freshness="reconnecting", effective_status_reason=str(exc)
                    )
                )
                await recover_http(app, key)
            else:
                store.put(
                    replace(
                        row,
                        effective_status="interrupted",
                        connection_freshness="disconnected",
                        effective_status_reason=str(exc),
                        result={"error": str(exc)},
                        notify_pending=not bool(row.consumed_at),
                    )
                )
    finally:
        row = store.get(key)
        if executor is not None and row is not None:
            executor._accepted_task_handles.discard(row.handle)
            if (
                executor._close_requested
                and not executor._accepted_task_handles
                and not executor._pending_task_submissions
            ):
                await executor.aclose(force=True)
                task_supervisor(app).retained_executors.discard(executor)
