"""Data-only conversion of obsolete transfer-task fields into existing Core history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from clio_agent.arc.task_types import TaskAlias
from clio_agent.gact.storage.models import TransferOperation
from clio_agent.gact.task_migration_inputs import _unresolved

RECEIPT_EVENT = "task.migration.source_operation"
_OBSOLETE = frozenset({"task_handle", "owner_session_id", "invocation_id"})


def source_operation_identity(source_id: str, operation_id: str) -> str:
    """Identify the source-scoped origin of a historical operation alias."""
    return "legacy-source-operation:" + json.dumps([source_id, operation_id], separators=(",", ":"))


@dataclass(frozen=True)
class LegacyStorageOperation:
    """One original row and its validated candidate; this never executes work."""

    identifier: str
    original_body: str
    original: dict[str, Any]
    cleaned_body: str

    @property
    def receipt_id(self) -> str:
        """Keep repeated startup conversion stable across a failed SQLite prune."""
        return (
            "migration:source-operation:" + hashlib.sha256(self.original_body.encode()).hexdigest()
        )


def read_legacy_operations(app: Any) -> tuple[LegacyStorageOperation, ...]:
    """Validate raw storage rows before Core writes or strict operation decoding."""
    store = getattr(getattr(app.state, "connected_storage", None), "store", None)
    if store is None:
        return ()
    with store.transaction() as db:
        rows = db.execute(
            "SELECT id, body FROM records WHERE kind='operation' AND "
            "(json_type(body, '$.task_handle') IS NOT NULL OR "
            "json_type(body, '$.owner_session_id') IS NOT NULL OR "
            "json_type(body, '$.invocation_id') IS NOT NULL)"
        ).fetchall()
    result = []
    for identifier, body in rows:
        original = json.loads(body)
        if not isinstance(original, dict):
            raise ValueError("Historical source operation is not an object")
        cleaned = {key: value for key, value in original.items() if key not in _OBSOLETE}
        operation = TransferOperation.model_validate(cleaned)
        if operation.id != identifier:
            raise ValueError("Historical operation identity disagrees with its storage key")
        result.append(
            LegacyStorageOperation(
                identifier,
                body,
                original,
                json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            )
        )
    return tuple(result)


def legacy_operation_task(
    row: LegacyStorageOperation, sessions: dict[str, Any]
) -> dict[str, Any] | None:
    """Describe missing historical work only when its original owner is established."""
    original = row.original
    values = [original.get(key) for key in ("task_handle", "owner_session_id", "invocation_id")]
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    if original["owner_session_id"] not in sessions:
        return None
    indexing = original["kind"] == "indexing"
    return {
        "handle": original["task_handle"],
        "id": original["id"],
        "invocation_id": original["invocation_id"],
        "kind": "Indexing" if indexing else "Download",
        "tool": "connected_data_index" if indexing else "connected_data_download",
        "status": original.get("state", "unknown"),
        "description": original.get("description") or None,
        "created_at": original.get("created_at"),
        "updated_at": original.get("updated_at"),
        "cancel_requested": original.get("cancel_requested", False),
        "source_id": original["source_id"],
        "legacy_storage_operation": original,
    }


def finish_legacy_operations(
    app: Any, rows: tuple[LegacyStorageOperation, ...], receipts: set[str]
) -> None:
    """Confirm retrievable Core evidence and association before conditional pruning."""
    if not rows:
        return
    history, store = app.state.task_history, app.state.connected_storage.store
    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    every = app.state.sessions.select(PageQuery(archived=None))  # a migration reads all
    sessions = {session.id: session for session in every}
    for row in rows:
        with history.serialized():
            original = row.original
            sid, handle = original.get("owner_session_id"), original.get("task_handle")
            session = sessions.get(sid) if isinstance(sid, str) else None
            resolved = (
                history.resolve_handle(handle) if isinstance(handle, str) and handle else None
            )
            task = history.task_snapshot(resolved) if resolved is not None else None
            association = bool(
                session is not None
                and task is not None
                and task.kind in {"Download", "Indexing", "MCP"}
                and task.resource_id in {None, original["source_id"]}
                and task.owner.session_id == sid
                and task.owner.human_id
                == (session.metadata.get("owner_human_id") or app.state.human_principal)
                and task.invocation_id == original.get("invocation_id")
            )
            evidence = {"operation": original, "original_body": row.original_body}
            if association and task is not None:
                reference = task.backend_observation
                body = history.read_content(reference) if reference is not None else None
                if not (
                    isinstance(body, dict)
                    and isinstance(body.get("record"), dict)
                    and body["record"].get("legacy_storage_operation") == original
                ):
                    reference = history.write_content(sid, evidence)
                assert reference is not None
            else:
                reference = _unresolved(
                    app,
                    row.receipt_id,
                    "source_operation_ownership_unavailable_or_conflicting",
                    evidence,
                )
            history.read_content(reference)  # Confirmation includes actual retrievability.
            if row.receipt_id not in receipts:
                history.record_source_operation_migration(
                    sid if session is not None else "_task_migration",
                    row.receipt_id,
                    {
                        "operation_id": row.identifier,
                        "source_id": original["source_id"],
                        "task_handle": task.handle if association and task is not None else None,
                        "content": reference.model_dump(mode="json"),
                        "association_confirmed": association,
                    },
                )
                receipts.add(row.receipt_id)
            if association and task is not None:
                history.record_alias(
                    TaskAlias(
                        alias=row.identifier,
                        handle=task.handle,
                        source_identity=source_operation_identity(
                            original["source_id"], row.identifier
                        ),
                    )
                )
        # Never hold the SQLite writer lock across Core calls or change timestamps.
        with store.transaction() as db:
            changed = db.execute(
                "UPDATE records SET body=? WHERE kind='operation' AND id=? AND body=?",
                (row.cleaned_body, row.identifier, row.original_body),
            ).rowcount
        if changed != 1:
            raise RuntimeError(
                "Historical source operation changed during migration; retry startup"
            )
