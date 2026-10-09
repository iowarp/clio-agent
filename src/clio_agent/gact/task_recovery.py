"""Reconnect durable task owners without submitting their operations again."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from clio_agent.errors import MCP_TASK_LEASE_HELD, ToolError
from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.task_projection import TERMINAL
from clio_agent.gact.task_supervisor import task_supervisor
from clio_agent.tools.mcp_task_records import TaskKey
from clio_agent.tools.task_receipt import (
    TaskAwareClient,
    TaskResultValidationError,
    record_invalid_result,
    validate_terminal,
)

logger = logging.getLogger(__name__)


class ReconnectingClient(TaskAwareClient):
    """Adopt the original task's negotiation without submitting initialize again."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **{**kwargs, "auto_initialize": False})


def start_task_recovery(app: Any) -> None:
    """Reproject storage and reconnect recoverable MCP work; never attach a recorded shell PID."""
    from clio_agent.gact.storage.task_adapter import recover_storage_handles

    if getattr(app.state, "connected_storage", None) is not None:
        recover_storage_handles(app)
    supervisor, store = task_supervisor(app), app_task_store(app)
    from clio_agent.gact.task_relay_owner import retain_relay_owner

    for task in app.state.agent_task_registry.snapshot():
        retain_relay_owner(app, task)
    from clio_agent.gact.task_backend_identity import relay_mirrors

    mirrored = relay_mirrors(app, app.state.agent_task_registry.snapshot(), store.list())
    for row in store.list():
        if not row.handle or row.holding_reason or row.handle in supervisor.drivers:
            continue
        if row.kind in {"Download", "Indexing"}:
            continue
        if row.key in mirrored:
            continue
        if (row.effective_status or row.status) in TERMINAL and row.status in TERMINAL:
            continue
        if row.kind == "MCP" and row.backend.get("transport") == "http":
            supervisor.supervise(row.handle, recover_http(app, row.key))
        else:
            store.put(
                replace(
                    row,
                    effective_status="interrupted",
                    connection_freshness="disconnected",
                    result={"error": "Original nonrecoverable execution owner was lost"},
                    notify_pending=not bool(row.consumed_at),
                )
            )


async def recover_http(app: Any, key: TaskKey) -> None:
    """Use tasks/get on the exact identity and original negotiation, never tools/call."""
    from mcp import types as mcp_types

    from clio_agent.gact.elicitation_bridge import make_elicitation_client
    from clio_agent.tools.mcp_config import transport_from_spec
    from clio_agent.tools.mcp_handlers import MCPInvocationContext
    from clio_agent.tools.mcp_task_extension import backend_identity
    from clio_agent.tools.mcp_tasks import cancel_task, resume_task, session_elicitation_callback
    from clio_agent.tools.task_observers import resolve_task_observer

    store, supervisor = app_task_store(app), task_supervisor(app)
    row = store.get(key)
    if row is None:
        raise ValueError("Task custody disappeared")
    locator = {"transport": row.backend["transport"], "url": row.backend["url"]}
    # Reuse current configured credentials only for an identical approved backend.
    for info in (getattr(app.state, "external_mcp_servers", {}) or {}).values():
        spec = info.get("spec", {})
        if spec.get("url") == locator["url"] and spec.get("transport") in {
            "http",
            "streamable-http",
        }:
            locator = dict(spec)
            break
    try:
        transport = transport_from_spec(locator)
        if backend_identity(transport).server_id != key.server_id:
            raise ValueError("Configured backend identity changed")
        if key.backend_session_id:
            transport.headers = {**transport.headers, "mcp-session-id": key.backend_session_id}
        negotiation = row.backend.get("negotiation") or {}
        if not negotiation:
            raise ValueError("Older task lacks original negotiation for safe reconnect")

        invocation = MCPInvocationContext(
            row.invocation_id, key.session_id, key.server_id, row.tool, task_id=key.task_id
        )
        context = make_elicitation_client(
            app,
            transport,
            key.server_id,
            row.tool,
            invocation=invocation,
            client_cls=ReconnectingClient,
        )
        async with context as client:
            model = (
                mcp_types.InitializeResult
                if negotiation["type"] == "initialize_result"
                else mcp_types.DiscoverResult
            )
            client.session.adopt(model.model_validate(negotiation["result"]))
            supervisor.cancellers[row.handle] = lambda: cancel_task(
                client.session, key, store=store
            )
            current = store.get(key)
            if current is not None and current.cancel_requested and not current.cancel_acknowledged:
                await cancel_task(client.session, key, store=store)
            while True:
                try:
                    await resume_task(
                        client.session,
                        key,
                        store=store,
                        timeout_seconds=None,
                        elicitation_callback=session_elicitation_callback(client.session),
                        on_poll=resolve_task_observer(key),
                        final_validator=lambda result: validate_terminal(row, result),
                    )
                    break
                except ToolError as exc:
                    if exc.details.get("reason") != MCP_TASK_LEASE_HELD:
                        raise
                    # A crash does not revoke a still-valid exclusive lease. Poll
                    # only after release/expiry; never replay or announce interruption.
                    expires = float(exc.details.get("lease_expires_at") or time.time())
                    await asyncio.sleep(min(1.0, max(0.01, expires - time.time())))
    except TaskResultValidationError as exc:
        logger.exception("Recovered task violated its output contract identity=%s", key.row_key)
        record_invalid_result(store, key, exc)
    except asyncio.CancelledError:
        pause_http_observation(store, key)
        raise
    except Exception as exc:
        logger.exception("Task reconnect failed identity=%s", key.row_key)
        _interrupted(store, key, str(exc))


def _interrupted(store: Any, key: TaskKey, reason: str) -> None:
    row = store.get(key)
    if row is not None:
        store.put(
            replace(
                row,
                effective_status="interrupted",
                connection_freshness="disconnected",
                effective_status_reason=reason,
                result={"error": reason},
                notify_pending=not bool(row.consumed_at),
            )
        )


def pause_http_observation(store: Any, key: TaskKey) -> None:
    """Disconnect recoverable work without announcing a false terminal outcome."""
    row = store.get(key)
    if row is not None:
        store.put(
            replace(
                row,
                connection_freshness="disconnected",
                effective_status_reason="Application observation paused; backend work may continue",
            )
        )
