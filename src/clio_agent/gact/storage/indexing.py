"""Persisted, cancellable folder indexing with atomic manifest publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING

from clio_agent.gact.storage.models import Manifest, SourceRecord, TransferOperation, now

if TYPE_CHECKING:
    from clio_agent.gact.storage.service import StorageService

logger = logging.getLogger(__name__)


def publish_index(
    service: StorageService, record: SourceRecord, manifest: Manifest, operation_id: str
) -> SourceRecord:
    """Commit the complete manifest, source pointer and settled operation together."""
    with service.store.transaction() as db:
        raw = db.execute(
            "SELECT body FROM records WHERE kind='operation' AND id=?", (operation_id,)
        ).fetchone()
        if raw is None:
            raise ValueError("Index operation disappeared")
        operation = TransferOperation.model_validate_json(raw[0])
        if operation.cancel_requested:
            raise InterruptedError("Folder indexing cancelled before publication")
        latest = db.execute(
            "SELECT body FROM records WHERE kind='source' AND id=?", (record.source.id,)
        ).fetchone()
        if latest is None:
            raise ValueError("Source disappeared before index publication")
        updated = SourceRecord.model_validate_json(latest[0])
        if (
            not updated.connected
            or updated.linked_manifest_id != record.linked_manifest_id
            or updated.source.root != record.source.root
            or updated.configuration != record.configuration
            or updated.link_access != record.link_access
        ):
            raise ValueError("Source changed while indexing")
        updated.linked_manifest_id = manifest.id
        settled = operation.model_copy(
            update={
                "state": "completed",
                "entries_done": len(manifest.entries),
                "manifest_id": manifest.id,
                "updated_at": now(),
            }
        )
        for kind, identifier, value in (
            ("manifest", manifest.id, manifest),
            ("source", updated.source.id, updated),
            ("operation", operation_id, settled),
        ):
            db.execute(
                "INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                (kind, identifier, value.model_dump_json()),
            )
    return updated


async def run_index(
    service: StorageService, record: SourceRecord, operation: TransferOperation
) -> None:
    """Enumerate off-loop, observe cancellation per entry, retain prior manifests on failure."""
    from clio_agent.gact.storage.linked import link_folder

    service.store.update_operation(operation.id, state="running")
    last_count = 0

    def progress(count: int) -> None:
        nonlocal last_count
        if count - last_count >= 32:
            service.store.update_operation(operation.id, entries_done=count)
            last_count = count

    def cancelled() -> bool:
        return service.store.get("operation", operation.id, TransferOperation).cancel_requested

    worker = asyncio.create_task(
        asyncio.to_thread(
            link_folder,
            service,
            record,
            progress=progress,
            cancelled=cancelled,
            operation_id=operation.id,
        )
    )
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        service.store.update_operation(operation.id, cancel_requested=True)
        try:
            await worker
        except Exception:
            # The enumerator has exited, including provider errors during shutdown.
            # Its outcome is recorded as interruption below, never a running owner.
            logger.exception(
                "Index worker exited during service interruption operation=%s", operation.id
            )
        current = service.store.get("operation", operation.id, TransferOperation)
        if current.state != "completed":
            service.store.update_operation(
                operation.id, state="interrupted", error="Service stopped indexing"
            )
        raise
    except InterruptedError as exc:
        logger.info("Index cancelled operation=%s reason=%s", operation.id, exc)
        service.store.update_operation(operation.id, state="cancelled", error=str(exc))
    except Exception as exc:
        logger.exception("Index failed operation=%s", operation.id)
        service.store.update_operation(operation.id, state="failed", error=str(exc))


def start_index(service: StorageService, record: SourceRecord) -> TransferOperation:
    """Deduplicate an equivalent active index; otherwise persist before starting enumeration."""
    signature = hashlib.sha256(
        json.dumps(
            [
                record.source.root,
                record.configuration.model_dump(mode="json"),
                record.link_access,
                record.linked_manifest_id,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    for operation in service.store.list("operation", TransferOperation):
        if (
            operation.source_id == record.source.id
            and operation.kind == "indexing"
            and operation.state in {"queued", "running"}
        ):
            if operation.index_signature != signature:
                raise ValueError("An index of a different source revision is already running")
            return operation
    operation = service.store.begin_operation(record.source.id, "indexing")
    operation = service.store.update_operation(operation.id, index_signature=signature)
    service.tasks[operation.id] = asyncio.create_task(run_index(service, record, operation))
    service.tasks[operation.id].add_done_callback(lambda _: service.tasks.pop(operation.id, None))
    return operation
