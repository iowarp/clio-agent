"""Durable local edits to a linked folder; publishing is an explicit user action."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from filelock import FileLock

from .adapters import file_hash, safe_child
from .models import (
    ChangeReview,
    FileEntry,
    LinkedEdit,
    LinkedEdits,
    Manifest,
    ReviewedChange,
    SourceRecord,
)
from .review import _preview_change
from .transfers import snapshot_revision

if TYPE_CHECKING:
    from .service import StorageService


def pending_edits(service: StorageService, record: SourceRecord) -> LinkedEdits:
    """Load the restart-safe journal for this link."""
    try:
        return service.store.get("linked-edits", record.source.id, LinkedEdits)
    except KeyError:
        return LinkedEdits(source_id=record.source.id, baseline_id=record.linked_manifest_id or "")


def _save_index(
    service: StorageService, record: SourceRecord, journal: LinkedEdits, entries: list[FileEntry]
) -> SourceRecord:
    rows = {row.path: row for row in entries}
    for name, edit in journal.changes.items():
        if edit.content_path is None:
            rows.pop(name, None)
        else:
            rows[name] = FileEntry(
                path=name,
                kind="file",
                size=Path(edit.content_path).stat().st_size,
                revision="local:" + str(edit.after_hash),
                sha256=edit.after_hash,
            )
            for parent in PurePosixPath(name).parents:
                if str(parent) != ".":
                    rows.setdefault(str(parent), FileEntry(path=str(parent), kind="directory"))
    manifest = Manifest(
        id="link_" + uuid.uuid4().hex,
        source_id=record.source.id,
        revision=snapshot_revision(list(rows.values())),
        entries=list(rows.values()),
        hashes={},
    )
    record.linked_manifest_id = manifest.id
    with service.store.transaction() as db:
        for kind, key, value in (
            ("manifest", manifest.id, manifest),
            ("linked-edits", record.source.id, journal),
            ("source", record.source.id, record),
        ):
            db.execute(
                "INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                (kind, key, value.model_dump_json()),
            )
    return record


def stage_edit(
    service: StorageService,
    record: SourceRecord,
    name: str,
    content: BinaryIO | None,
    before: Path | None,
) -> SourceRecord:
    """Retain a local edit without performing any upstream mutation; caller holds source lock."""
    FileEntry(path=name, kind="file")
    if record.linked_access != "publish_later" or not record.linked_manifest_id:
        raise PermissionError("This link does not allow local edits")
    journal = pending_edits(service, record)
    manifest = service.store.get("manifest", record.linked_manifest_id, Manifest)
    destination = None
    digest = None
    if content is not None:
        destination = service.store.root / record.source.id / ("edit_" + uuid.uuid4().hex)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("xb") as writer:
                size = 0
                while chunk := content.read(1024 * 1024):
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        raise ValueError("This file exceeds the 512 MB linked-write limit")
                    writer.write(chunk)
            digest = file_hash(destination)
            os.chmod(destination, 0o400)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    original = journal.changes.get(name)
    journal.changes[name] = LinkedEdit(
        before_hash=original.before_hash if original else file_hash(before) if before else None,
        before_path=original.before_path if original else str(before) if before else None,
        after_hash=digest,
        content_path=str(destination) if destination else None,
    )
    return _save_index(service, record, journal, manifest.entries)


def staged_file(service: StorageService, record: SourceRecord, name: str) -> Path | None:
    """Expose staged bytes under the current protected link revision."""
    edit = pending_edits(service, record).changes.get(name)
    if edit is None:
        return None
    if edit.content_path is None:
        raise FileNotFoundError(name)
    content = Path(edit.content_path)
    if file_hash(content) != edit.after_hash:
        raise ValueError("The staged file changed outside CLIO; discard it and try again")
    target = safe_child(
        service.store.root / record.source.id / str(record.linked_manifest_id), name
    )
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(content, target)
        os.chmod(target, 0o400)
    return target


def review_edits(service: StorageService, record: SourceRecord) -> ChangeReview:
    """Compare only edited files with their retained originals and current upstream bytes."""
    from .linked import linked_adapter

    if not record.linked_manifest_id or record.linked_access != "publish_later":
        raise ValueError("Only linked folders with local edits can be published")
    journal = pending_edits(service, record)
    changes = []
    with linked_adapter(service, record, refresh=True) as adapter:
        upstream = {row.path: row for row in adapter.entries()}
        for name, edit in sorted(journal.changes.items()):
            current = upstream.get(name)
            digest = None
            if current:
                if current.kind != "file" or current.size > 512 * 1024 * 1024:
                    raise ValueError("The original changed type or exceeds the linked-write limit")
                hasher = hashlib.sha256()
                total = 0
                with adapter.open_read(current) as reader:
                    while chunk := reader.read(1024 * 1024):
                        total += len(chunk)
                        if total > 512 * 1024 * 1024:
                            raise ValueError("The original exceeds the linked-write limit")
                        hasher.update(chunk)
                digest = hasher.hexdigest()
            missing = service.store.root / ".no-original"
            preview, note = _preview_change(
                Path(edit.before_path) if edit.before_path else missing,
                Path(edit.content_path) if edit.content_path else missing,
                name,
            )
            changes.append(
                ReviewedChange(
                    path=name,
                    kind="delete"
                    if edit.after_hash is None
                    else "add"
                    if edit.before_hash is None
                    else "modify",
                    local_hash=edit.after_hash,
                    upstream_revision=current.revision if current else None,
                    conflict=digest != edit.before_hash,
                    preview=preview,
                    preview_note=note,
                )
            )
    review = ChangeReview(
        id="review_" + uuid.uuid4().hex,
        source_id=record.source.id,
        manifest_id=record.linked_manifest_id,
        changes=changes,
    )
    service.store.put("review", review.id, review)
    return review


def publish_edits(
    service: StorageService, record: SourceRecord, review_id: str, selected: list[str]
) -> str:
    """Publish exactly the reviewed selection; keep unselected edits and partial-failure receipts."""
    from .linked import linked_adapter

    lock_root = service.store.root / ".write-locks"
    lock_root.mkdir(exist_ok=True)
    with FileLock(lock_root / (record.source.id + ".lock"), timeout=30):
        record = service.get(record.source.workspace_id or "", record.source.id)
        service.require_idle(record)
        if not record.linked_manifest_id or record.linked_access != "publish_later":
            raise ValueError("Only linked folders with local edits can be published")
        review = service.store.get("review", review_id, ChangeReview)
        if review.source_id != record.source.id or review.manifest_id != record.linked_manifest_id:
            raise ValueError("This link changed; review again before publishing")
        rows = {row.path: row for row in review.changes}
        if (
            not selected
            or len(set(selected)) != len(selected)
            or any(name not in rows for name in selected)
        ):
            raise ValueError("Select distinct files from this review")
        latest = {row.path: row for row in review_edits(service, record).changes}
        journal = pending_edits(service, record)
        for name in selected:
            if (
                name not in latest
                or latest[name].conflict
                or rows[name].conflict
                or (latest[name].local_hash, latest[name].upstream_revision)
                != (rows[name].local_hash, rows[name].upstream_revision)
            ):
                raise ValueError("Files changed after review; review again before publishing")
            edit = journal.changes[name]
            if edit.content_path and file_hash(Path(edit.content_path)) != edit.after_hash:
                raise ValueError("The staged file changed after review")
        operation = service.store.begin_operation(record.source.id, "apply")
        service.store.update_operation(operation.id, state="running", selected_paths=selected)
        applied: list[str] = []
        try:
            with linked_adapter(service, record, refresh=True) as adapter:
                updates = [
                    (
                        name,
                        Path(journal.changes[name].content_path or "")
                        if journal.changes[name].content_path
                        else None,
                        rows[name].upstream_revision,
                    )
                    for name in selected
                ]
                if record.source.provider == "github":
                    adapter.apply_many(updates)
                    applied.extend(selected)
                    for name in selected:
                        journal.changes.pop(name)
                    service.store.put("linked-edits", record.source.id, journal)
                    service.store.update_operation(operation.id, applied_paths=applied)
                else:
                    for name, content, revision in updates:
                        adapter.apply(name, content, revision)
                        applied.append(name)
                        journal.changes.pop(name)
                        service.store.put("linked-edits", record.source.id, journal)
                        service.store.update_operation(operation.id, applied_paths=applied)
                _save_index(service, record, journal, adapter.entries())
            service.store.update_operation(operation.id, state="completed")
        except Exception as exc:
            if applied:
                try:
                    with linked_adapter(service, record, refresh=True) as adapter:
                        _save_index(service, record, journal, adapter.entries())
                except Exception:
                    logging.exception(
                        "reason=linked_index_refresh_failed Published files were recorded, "
                        "but the linked index could not be refreshed",
                    )
            service.store.update_operation(
                operation.id, state="failed", error=str(exc), applied_paths=applied
            )
            # Already published paths must not be retried, even if refreshing the provider fails.
            raise
        return operation.id


def discard_edits(service: StorageService, record: SourceRecord) -> SourceRecord:
    """Discard pending local edits while retaining immutable attachment evidence."""
    from .linked import linked_adapter

    lock_root = service.store.root / ".write-locks"
    lock_root.mkdir(exist_ok=True)
    with FileLock(lock_root / (record.source.id + ".lock"), timeout=30):
        record = service.get(record.source.workspace_id or "", record.source.id)
        service.require_idle(record)
        with linked_adapter(service, record, refresh=True) as adapter:
            entries = adapter.entries()
        return _save_index(
            service, record, LinkedEdits(source_id=record.source.id, baseline_id=""), entries
        )
