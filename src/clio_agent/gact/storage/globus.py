"""Native Globus collection jobs with durable, idempotent submission and reconciliation."""

from __future__ import annotations

import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

import globus_sdk
from clio_schemas.connected_resources import SourceCapabilities

from clio_agent.gact.storage.adapters import LocalSource
from clio_agent.gact.storage.models import FileEntry, SourceRecord, TransferOperation
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.transfers import materialize, remove_owned_tree


def _wire_payload(value: Any) -> Any:
    """Persist the SDK payload's wire values, excluding its public MISSING sentinel."""
    if isinstance(value, dict):
        return {
            key: _wire_payload(item)
            for key, item in value.items()
            if item is not globus_sdk.MISSING
        }
    if isinstance(value, list):
        return [_wire_payload(item) for item in value if item is not globus_sdk.MISSING]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class GlobusSource:
    """Use Globus to transfer collections; never simulate a mount with polling sync."""

    capabilities = SourceCapabilities(
        search=True,
        native_transfer=True,
        supported_modes=["read_only"],
        unavailable_reasons={
            "working_copy": "This collection exposes transfer jobs without conditional revision updates; use a read-only import.",
            "write_enabled": "Globus transfers do not expose a writable operating-system folder on this node.",
        },
    )

    def __init__(self, record: SourceRecord, token: str, *, client: Any = None) -> None:
        self.record = record
        UUID(record.configuration.collection_id)
        if (
            not PurePosixPath(record.source.root).is_absolute()
            or ".." in PurePosixPath(record.source.root).parts
        ):
            raise ValueError("Select an absolute path within the Globus collection")
        self.client = client or globus_sdk.TransferClient(
            authorizer=globus_sdk.AccessTokenAuthorizer(token)
        )

    def entries(self) -> list[FileEntry]:
        """Browse selected collection descendants, with explicit limits and no symlink following."""
        rows: list[FileEntry] = []
        pending = [""]
        try:
            while pending:
                parent = pending.pop()
                offset = 0
                while True:
                    listing = list(
                        self.client.operation_ls(
                            self.record.configuration.collection_id,
                            path=posixpath.join(self.record.source.root, parent),
                            limit=1000,
                            offset=offset,
                        )
                    )
                    for metadata in listing:
                        name = str(metadata["name"])
                        if "/" in name or "\\" in name:
                            raise ValueError("Collection returned an unsafe filename")
                        path = posixpath.join(parent, name)
                        if metadata["type"] not in {"file", "dir"}:
                            raise ValueError(
                                "Select regular files and folders; collection links are not imported"
                            )
                        kind: Literal["directory", "file"] = (
                            "directory" if metadata["type"] == "dir" else "file"
                        )
                        rows.append(
                            FileEntry(
                                path=path,
                                kind=kind,
                                size=int(metadata.get("size", 0)),
                                revision=str(metadata.get("last_modified", "")),
                            )
                        )
                        if len(rows) > 100_000:
                            raise ValueError(
                                "Select a smaller collection folder (limit 100,000 entries)"
                            )
                        if kind == "directory":
                            pending.append(path)
                    if len(listing) < 1000:
                        break
                    offset += len(listing)
        except globus_sdk.GlobusAPIError as exc:
            raise ValueError(
                "Globus could not list this collection; check sign-in, collection consent and path access"
            ) from exc
        return sorted(rows, key=lambda row: row.path)

    def submit(self, store: SourceStore, operation: TransferOperation) -> TransferOperation:
        """Save a submission ID before sending, so retry cannot create a second native job."""
        config = self.record.configuration
        UUID(config.destination_collection_id)
        local_root = Path(config.destination_local_root)
        remote_root = PurePosixPath(config.destination_collection_root)
        if not local_root.is_absolute() or not local_root.is_dir() or not remote_root.is_absolute():
            raise ValueError(
                "Configure the destination collection's local and collection root paths on this CLIO"
            )
        owner = store.root / self.record.source.id
        stage = owner / ("native-" + operation.id)
        relative = stage.resolve().relative_to(local_root.resolve()).as_posix()
        destination = str(remote_root / relative)
        try:
            if operation.native_request is None:
                owner.mkdir(parents=True, exist_ok=True)
                submission = str(self.client.get_submission_id()["value"])
                transfer = globus_sdk.TransferData(
                    config.collection_id,
                    config.destination_collection_id,
                    submission_id=submission,
                    label="CLIO " + operation.id,
                    sync_level="checksum",
                    verify_checksum=True,
                    encrypt_data=True,
                    deadline=datetime.now(timezone.utc) + timedelta(hours=4),
                    fail_on_quota_errors=True,
                    skip_source_errors=False,
                    delete_destination_extra=False,
                    notify_on_succeeded=False,
                    notify_on_failed=False,
                    notify_on_inactive=False,
                )
                transfer.add_item(self.record.source.root, destination, recursive=True)
                operation = store.update_operation(
                    operation.id, native_request=_wire_payload(transfer), state="running"
                )
            if operation.native_job_id is None:
                if operation.native_request is None:
                    raise ValueError("The Globus submission receipt is missing")
                result = self.client.submit_transfer(operation.native_request)
                operation = store.update_operation(
                    operation.id, native_job_id=str(result["task_id"]), state="running", error=None
                )
            return operation
        except globus_sdk.GlobusAPIError as exc:
            raise ValueError(
                "Globus transfer could not be submitted; retry uses the same submission ID"
            ) from exc

    def poll(
        self, store: SourceStore, operation: TransferOperation, workspace_root: Path
    ) -> TransferOperation:
        """Reconcile native truth, publishing inputs only after transfer and local validation."""
        if operation.native_job_id is None:
            operation = self.submit(store, operation)
        if operation.native_job_id is None:
            raise ValueError("Globus returned no transfer task identity")
        try:
            if operation.cancel_requested:
                self.client.cancel_task(operation.native_job_id)
            task = self.client.get_task(operation.native_job_id)
        except globus_sdk.GlobusAPIError as exc:
            raise ValueError(
                "Globus task status is unavailable; no completion or cleanup was assumed"
            ) from exc
        operation = store.update_operation(
            operation.id, bytes_done=int(task.get("bytes_transferred", 0))
        )
        owner = store.root / self.record.source.id
        stage = owner / ("native-" + operation.id)
        if task["status"] == "SUCCEEDED":
            if operation.cancel_requested:
                store.update_operation(
                    operation.id,
                    state="cancelled",
                    error="Transfer completed before cancellation; the unexposed local staging copy was removed",
                )
                remove_owned_tree(stage, owner)
                return store.get("operation", operation.id, TransferOperation)
            # The collection mapping must lead to real files on this CLIO; a
            # successful cloud job alone is not successful local materialization.
            adapter = LocalSource(str(stage))
            materialize(store, self.record, adapter, workspace_root, operation)
            remove_owned_tree(stage, owner)
        elif task["status"] == "FAILED":
            state = "cancelled" if operation.cancel_requested else "failed"
            store.update_operation(
                operation.id,
                state=state,
                error="Globus transfer cancelled"
                if operation.cancel_requested
                else "Globus transfer failed; inspect the native task before retrying",
            )
            remove_owned_tree(stage, owner)
        current = store.get("operation", operation.id, TransferOperation)
        if current.state in {"failed", "cancelled"}:
            self.record.source = self.record.source.model_copy(
                update={"materialization": "stale" if self.record.manifest_id else "failed"}
            )
            store.put("source", self.record.source.id, self.record)
        return store.get("operation", operation.id, TransferOperation)
