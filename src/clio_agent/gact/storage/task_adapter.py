"""Shared task handles over storage-owned operations; no second transfer owner."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any
from uuid import uuid4

from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.storage.models import SourceRecord, TransferOperation
from clio_agent.gact.task_projection import TERMINAL
from clio_agent.gact.task_supervisor import task_supervisor
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord
from clio_agent.tools.task_call_context import require_admission

logger = logging.getLogger(__name__)


def validate_task_owner(app: Any, wid: str, sid: str) -> None:
    """Validate conversation ownership and admission before accepting storage work."""
    if not sid:
        return
    owner = app.state.sessions.get(sid)
    if owner is None or owner.workspace_id != wid:
        raise ValueError("Source task conversation does not belong to this workspace")
    try:
        require_admission(app, sid)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def storage_handle(
    app: Any, sid: str, operation: TransferOperation, description: str, invocation_id: str = ""
) -> dict[str, Any]:
    """Persist complete task ownership before acknowledging an accepted storage operation."""
    service = app.state.connected_storage
    if operation.owner_session_id and operation.owner_session_id != sid:
        raise ValueError("Indexing is already owned by another conversation")
    try:
        return _record_storage_handle(app, sid, operation, description, invocation_id)
    except Exception as exc:
        # Submission and custody recording run synchronously on the application
        # loop. A newly queued worker cannot start between them. Stop that owner
        # before yielding; an already running deduplicated owner cooperatively
        # cancels and retains custody until its normal cleanup settles.
        logger.exception("Storage acceptance recording failed operation=%s", operation.id)
        current = service.store.get("operation", operation.id, TransferOperation)
        service.store.update_operation(operation.id, cancel_requested=True)
        owner = service.tasks.get(operation.id)
        if current.state == "queued" and owner is not None:
            owner.cancel()
            service.store.update_operation(
                operation.id, state="failed", error=f"Task acceptance was not recorded: {exc}"
            )
        raise RuntimeError(
            f"Storage operation {operation.id} was not acknowledged: durable task recording failed"
        ) from exc


def _record_storage_handle(
    app: Any, sid: str, operation: TransferOperation, description: str, invocation_id: str
) -> dict[str, Any]:
    service = app.state.connected_storage
    if not operation.task_handle:
        operation = service.store.update_operation(
            operation.id,
            task_handle="task_" + uuid4().hex,
            owner_session_id=sid,
            description=description,
            invocation_id=invocation_id,
        )
    key = TaskKey("storage:" + service.store.clio_id, sid, operation.id)
    store = app_task_store(app)
    kind = "Indexing" if operation.kind == "indexing" else "Download"
    if store.get(key) is None:
        store.put(
            TaskRecord(
                key=key,
                handle=operation.task_handle,
                kind=kind,
                description=description,
                invocation_id=invocation_id,
                owner_agent=_owner_agent(),
                tool="connected_data_connect" if kind == "Indexing" else "connected_data_download",
                backend={
                    "operation_id": operation.id,
                    "source_id": operation.source_id,
                    "transport": "storage",
                    "placement": "local",
                },
                status=operation.state,
                created_at=operation.created_at,
            )
        )
    supervisor = task_supervisor(app)
    saved = store.get(key)
    if saved is None or saved.holding_reason or saved.handle != operation.task_handle:
        raise RuntimeError("Storage task acceptance could not be durably recorded")
    _sync_storage(app, key)
    if operation.task_handle not in supervisor.drivers and operation.state not in TERMINAL:

        async def cancel() -> None:
            service.store.update_operation(operation.id, cancel_requested=True)

        supervisor.supervise(operation.task_handle, _observe_storage(app, key), cancel)
    return {
        "accepted": True,
        "handle": operation.task_handle,
        "kind": kind,
        "description": description,
        "status": (store.get(key).effective_status if store.get(key) else operation.state),
        "operation_id": operation.id,
    }


async def _observe_storage(app: Any, key: TaskKey) -> None:
    while True:
        if _sync_storage(app, key):
            return
        await asyncio.sleep(0.2)


def _owner_agent() -> str:
    from clio_agent.gact.context import active_react_scope

    return active_react_scope()


def _sync_storage(app: Any, key: TaskKey) -> bool:
    """Project durable raw state while delaying effective settlement until owner cleanup finishes."""
    service, store = app.state.connected_storage, app_task_store(app)
    operation = service.store.get("operation", key.task_id, TransferOperation)
    row = store.get(key)
    if row is None:
        raise RuntimeError("Storage task mapping disappeared")
    owner = service.tasks.get(operation.id)
    settled = operation.state in TERMINAL and (owner is None or owner.done())
    wire = operation.model_dump(mode="json", exclude={"native_request"})
    source = service.store.get("source", operation.source_id, SourceRecord)
    backend = {
        **row.backend,
        "progress": {
            "bytes_done": operation.bytes_done,
            "bytes_total": operation.bytes_total,
            "entries_done": operation.entries_done,
        },
    }
    result = {
        "operation": wire,
        "source": source.source.model_dump(mode="json"),
        "manifest_id": operation.manifest_id or source.linked_manifest_id or source.manifest_id,
    }
    updated = replace(
        row,
        backend=backend,
        status=operation.state,
        effective_status=operation.state if settled else "running",
        result=result if settled else None,
        cancel_requested=operation.cancel_requested,
        notify_pending=settled and not bool(row.consumed_at),
    )
    if updated != row:
        store.put(updated)
    return settled


def recover_storage_handles(app: Any) -> None:
    """Rejoin recorded storage owners after reconciliation without restarting local operations."""
    for operation in app.state.connected_storage.store.list("operation", TransferOperation):
        if operation.task_handle and operation.owner_session_id:
            storage_handle(
                app,
                operation.owner_session_id,
                operation,
                operation.description,
                operation.invocation_id,
            )
