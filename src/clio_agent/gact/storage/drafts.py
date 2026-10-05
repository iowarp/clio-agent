"""Rollback workspace data introduced by source attachments removed before sending."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Callable

from pydantic import Field

from clio_agent.gact.resource_custody import ResourceRecord, ResourceStore
from clio_agent.gact.storage.linked_changes import pending_edits
from clio_agent.gact.storage.models import (
    LinkedEdits,
    Manifest,
    SourceRecord,
    StorageModel,
    TransferOperation,
)
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.transfers import local_hashes, remove_owned_tree
from clio_agent.platform_paths import copytree_extended


class SourceDraft(StorageModel):
    """Shared rollback boundary for concurrent unsent attachments of one source."""

    id: str
    before: SourceRecord
    edits_before: LinkedEdits | None = None
    leases: list[str] = Field(default_factory=list)
    resources_before: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    manifests_before: list[str] = Field(default_factory=list)
    operations_before: list[str] = Field(default_factory=list)
    state: str = "pending"


class SourceDrafts:
    """Retain sent evidence and roll back only data owned by an abandoned draft."""

    def __init__(self, service: StorageService, resources: ResourceStore) -> None:
        self.service, self.resources = service, resources
        self.lock = threading.RLock()

    def begin(self, record: SourceRecord) -> str:
        """Join an unsent source change, or capture the current workspace baseline."""
        with self.lock:
            rows = self.service.store.list("source-draft", SourceDraft)
            draft = next(
                (
                    row
                    for row in rows
                    if row.before.source.id == record.source.id and row.state == "pending"
                ),
                None,
            )
            if draft is not None and not draft.leases:
                self._cleanup(draft)
                if draft.state == "pending":
                    raise ValueError("Wait for the removed attachment's download to stop")
                record = self.service.get(record.source.workspace_id or "", record.source.id)
                draft = None
            if draft is None:
                draft = SourceDraft(
                    id="draft_" + uuid.uuid4().hex,
                    before=record.model_copy(deep=True),
                    edits_before=pending_edits(self.service, record),
                    resources_before=[
                        row.id for row in self.resources.list(record.source.workspace_id or "")
                    ],
                    manifests_before=[
                        row.id
                        for row in self.service.store.list("manifest", Manifest)
                        if row.source_id == record.source.id
                    ],
                    operations_before=[
                        row.id
                        for row in self.service.store.list("operation", TransferOperation)
                        if row.source_id == record.source.id
                    ],
                )
            lease = "attachment_" + uuid.uuid4().hex
            draft.leases.append(lease)
            self.service.store.put("source-draft", draft.id, draft)
            return lease

    def _find(self, source_id: str, lease: str) -> SourceDraft:
        for row in self.service.store.list("source-draft", SourceDraft):
            if row.before.source.id == source_id and lease in row.leases:
                return row
        raise ValueError("This attachment was removed; add it again")

    def prepare(
        self, source_id: str, lease: str, action: Callable[[], ResourceRecord]
    ) -> ResourceRecord:
        """Serialize receipt creation with removal so a late receipt cannot recreate data."""
        with self.lock:
            draft = self._find(source_id, lease)
            resource = action()
            if resource.id not in draft.resources:
                draft.resources.append(resource.id)
                self.service.store.put("source-draft", draft.id, draft)
            return resource

    def change(self, source_id: str, lease: str = "") -> None:
        """Validate a draft operation; explicit management outside it keeps prior data."""
        with self.lock:
            if lease:
                self._find(source_id, lease)
                return
            for draft in self.service.store.list("source-draft", SourceDraft):
                if draft.before.source.id == source_id and draft.state == "pending":
                    draft.state = "kept"
                    self.service.store.put("source-draft", draft.id, draft)

    def retain_resources(self, identifiers: set[str]) -> None:
        """Commit staged source data when its resource is accepted into a message."""
        with self.lock:
            for draft in self.service.store.list("source-draft", SourceDraft):
                if draft.state == "pending" and identifiers.intersection(draft.resources):
                    draft.state = "kept"
                    self.service.store.put("source-draft", draft.id, draft)

    def uploaded_resources(self, source_id: str, lease: str, identifiers: list[str]) -> None:
        """Include uniquely owned upload receipts in the folder's rollback boundary."""
        with self.lock:
            draft = self._find(source_id, lease)
            draft.resources.extend(identifiers)
            draft.resources_before = [
                key for key in draft.resources_before if key not in identifiers
            ]
            self.service.store.put("source-draft", draft.id, draft)

    def finish(self, source_id: str, lease: str, *, keep: bool = False) -> None:
        """Release one attachment; removing the last unsent one rolls back its data."""
        with self.lock:
            try:
                draft = self._find(source_id, lease)
            except ValueError:
                self.cleanup()  # Retry cleanup after a file lock or a lost response.
                return
            if keep:
                draft.state = "kept"
            draft.leases.remove(lease)
            self.service.store.put("source-draft", draft.id, draft)
            if not draft.leases and draft.state == "pending":
                self._cleanup(draft)

    def cleanup(self) -> None:
        """Finish abandoned copies after cancellation, completion, or server restart."""
        with self.lock:
            for draft in self.service.store.list("source-draft", SourceDraft):
                if draft.state == "pending" and not draft.leases:
                    self._cleanup(draft)

    def _cleanup(self, draft: SourceDraft) -> None:
        source_id = draft.before.source.id
        operations = [
            row
            for row in self.service.store.list("operation", TransferOperation)
            if row.source_id == source_id and row.id not in draft.operations_before
        ]
        active = [
            row
            for row in operations
            if row.state in {"queued", "running"}
            or (row.native_request and row.state == "interrupted")
        ]
        if active:
            for operation in active:
                self.service.store.update_operation(operation.id, cancel_requested=True)
            return
        record = self.service.store.get("source", source_id, SourceRecord)
        before = draft.before
        workspace_id = before.source.workspace_id or ""
        # Only this source's opaque, CLIO-owned paths are eligible for deletion.
        if not record.download_read_only and record.source.local_path != before.source.local_path:
            working = Path(record.source.local_path or "")
            expected = Path(record.workspace_root) / "connected-data" / source_id
            if working.resolve() != expected.resolve():
                raise ValueError("The draft copy's ownership changed; cleanup was stopped")
            self._remove_working(record, expected)
        elif not record.download_read_only and record.manifest_id != before.manifest_id:
            working = Path(record.source.local_path or "")
            expected = Path(record.workspace_root) / "connected-data" / source_id
            if working.resolve() != expected.resolve():
                raise ValueError("The draft copy's ownership changed; cleanup was stopped")
            self._remove_working(record, expected)
            if before.manifest_id and before.source.local_path:
                copytree_extended(
                    self.service.store.root / source_id / before.manifest_id, expected
                )
        for identifier in draft.resources:
            if identifier not in draft.resources_before:
                self.resources.delete(workspace_id, identifier)
        for manifest in self.service.store.list("manifest", Manifest):
            if manifest.source_id == source_id and manifest.id not in draft.manifests_before:
                remove_owned_tree(
                    self.service.store.root / source_id / manifest.id,
                    self.service.store.root / source_id,
                )
        source_root = self.service.store.root / source_id
        if source_root.is_dir() and not any(source_root.iterdir()):
            source_root.rmdir()
        record.manifest_id = before.manifest_id
        record.linked_manifest_id = before.linked_manifest_id
        if draft.edits_before is not None:
            self.service.store.put("linked-edits", source_id, draft.edits_before)
        record.download_access = before.download_access
        record.link_access = before.link_access
        if before.origin == "desktop_upload" and not before.manifest_id:
            record.removed = True
        record.source = record.source.model_copy(
            update={
                key: getattr(before.source, key)
                for key in ("local_path", "materialization", "revision", "operation_id")
            }
        )
        self.service.store.put("source", source_id, record)
        draft.state = "removed"
        self.service.store.put("source-draft", draft.id, draft)

    def _remove_working(self, record: SourceRecord, path: Path) -> None:
        if record.manifest_id and path.exists():
            manifest = self.service.store.get("manifest", record.manifest_id, Manifest)
            if local_hashes(path) != manifest.hashes:
                raise ValueError("This draft folder has edited files; remove its copy from Sources")
        remove_owned_tree(path, path.parent)


def retain_message_sources(app: object, message: object) -> None:
    """Keep source data at the existing durable message-acceptance boundary."""
    if getattr(message, "role", None) == "user":
        identifiers = {
            part.resource_id
            for part in getattr(message, "parts", [])
            if getattr(part, "type", None) == "resource_ref"
        }
        retain_attachment_resources(app, identifiers)


def retain_attachment_resources(app: object, identifiers: set[str]) -> None:
    """Retain both ordinary uploads and source snapshots for accepted message parts."""
    state = getattr(app, "state", None)
    resources = getattr(state, "resource_store", None)
    if resources is not None:
        resources.retain_attachments(identifiers)
    drafts = getattr(state, "source_drafts", None)
    service = getattr(state, "connected_storage", None)
    if state is not None and drafts is None and service is not None and resources is not None:
        drafts = SourceDrafts(service, resources)
        state.source_drafts = drafts
        service.transfer_settled = drafts.cleanup
    if drafts is not None:
        drafts.retain_resources(identifiers)
