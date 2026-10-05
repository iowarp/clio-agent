"""Permission-checked fsspec access with retained originals before upstream writes.

This is a virtual filesystem, not an OS mount. Provider IO remains in the existing
adapters; downloaded workspace copies never pass through this write boundary.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, cast

from filelock import FileLock
from fsspec import AbstractFileSystem

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.adapters import file_hash
from clio_agent.gact.storage.linked import link_folder, linked_adapter, linked_file
from clio_agent.gact.storage.models import FileEntry, Manifest, SourceRecord
from clio_agent.gact.storage.references import source_resource
from clio_agent.gact.storage.service import StorageService

MAX_WRITE = 512 * 1024 * 1024


class _TextFile(io.TextIOWrapper):
    """Do not publish partial text when a caller leaves the context with an error."""

    def __exit__(self, *exc: Any) -> None:
        if exc[0] is not None and isinstance(self.buffer, _PendingWrite):
            self.buffer.aborted = True
        super().__exit__(*exc)


class _PendingWrite(io.BufferedIOBase):
    """Stage bytes locally; publish only on successful explicit close."""

    def __init__(self, fs: SourceFileSystem, path: str, exclusive: bool) -> None:
        super().__init__()
        self.fs, self.path, self.exclusive = fs, path, exclusive
        self.buffer = tempfile.TemporaryFile(mode="w+b")
        self.aborted = False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.buffer.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.buffer.seek(offset, whence)

    def write(self, data: Any) -> int:
        if self.buffer.tell() + len(data) > MAX_WRITE:
            self.aborted = True
            raise ValueError("This file exceeds the 512 MB linked-write limit")
        return self.buffer.write(data)

    def flush(self) -> None:
        if not self.buffer.closed:
            self.buffer.flush()

    def close(self) -> None:
        if self.closed:
            return
        try:
            if not self.aborted:
                self.buffer.seek(0)
                self.fs.publish(self.path, cast(BinaryIO, self.buffer), exclusive=self.exclusive)
        finally:
            self.aborted = True
            super().close()
            self.buffer.close()

    def __exit__(self, *exc: Any) -> None:
        self.aborted = self.aborted or exc[0] is not None
        self.close()

    def __del__(self) -> None:
        self.aborted = True
        self.close()


class SourceFileSystem(AbstractFileSystem):
    """Use an approved link with durable access checks and immutable write evidence."""

    protocol = "clio_source"
    cachable = False

    def __init__(
        self,
        service: StorageService,
        workspace_id: str,
        source_id: str,
        resources: ResourceStore,
        *,
        revision: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.service, self.workspace_id, self.source_id = service, workspace_id, source_id
        self.resources = resources
        record = service.get(workspace_id, source_id)
        self.revision = revision or record.linked_manifest_id or ""
        self.last_operation: str | None = None

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        # Preserve absolute/traversal spellings so validation rejects them.
        return str(path).removeprefix("clio_source://")

    def _record(self, *, write: bool = False) -> SourceRecord:
        record = self.service.get(self.workspace_id, self.source_id)
        if not self.revision or record.linked_manifest_id != self.revision:
            raise ValueError("The folder link changed; open its current reference before writing")
        if write and record.linked_access == "read_only":
            raise PermissionError("This folder is read only. Ask the user to allow source edits.")
        return record

    def open(self, path: str, mode: str = "rb", **kwargs: Any) -> Any:
        """Retain normal fsspec binary/text access without publishing failed text writes."""
        if "b" not in mode:
            text_options = {
                key: kwargs.pop(key) for key in ("encoding", "errors", "newline") if key in kwargs
            }
            stream = super().open(path, mode.replace("t", "") + "b", **kwargs)
            return _TextFile(stream, **text_options)
        return super().open(path, mode, **kwargs)

    def ls(self, path: str = "", detail: bool = True, **kwargs: Any) -> Any:
        """List only metadata in the approved link revision."""
        self._record()
        path = self._strip_protocol(path)
        if path:
            FileEntry(path=path, kind="directory")
        manifest = self.service.store.get("manifest", self.revision, Manifest)
        prefix = path + "/" if path else ""
        rows = [
            {"name": row.path, "type": row.kind, "size": row.size}
            for row in manifest.entries
            if row.path.startswith(prefix) and "/" not in row.path[len(prefix) :]
        ]
        if detail:
            return rows
        return [row["name"] for row in rows]

    def _open(self, path: str, mode: str = "rb", **kwargs: Any) -> BinaryIO:
        FileEntry(path=path, kind="file")
        record = self._record(write=mode != "rb")
        if mode == "rb":
            return linked_file(self.service, record, path).open("rb")
        if mode not in {"wb", "xb"} or not kwargs.get("autocommit", True):
            raise ValueError(
                "Linked writes support complete file replacement, not append or transactions"
            )
        return cast(BinaryIO, _PendingWrite(self, path, mode == "xb"))

    def rm_file(self, path: str) -> None:
        """Remove one source file after retaining its immutable original."""
        self.publish(self._strip_protocol(path), None)

    def publish(self, path: str, content: BinaryIO | None, *, exclusive: bool = False) -> None:
        """Retain the original, reject stale evidence, then use the existing provider writer."""
        FileEntry(path=path, kind="file")
        self._record(write=True)
        store = self.service.store
        lock_root = store.root / ".write-locks"
        lock_root.mkdir(exist_ok=True)
        with FileLock(lock_root / (self.source_id + ".lock"), timeout=30):
            record = self._record(write=True)
            self.service.require_idle(record)
            manifest = store.get("manifest", self.revision, Manifest)
            previous = next((row for row in manifest.entries if row.path == path), None)
            if previous and previous.kind != "file":
                raise IsADirectoryError(path)
            if previous and exclusive:
                raise FileExistsError(path)
            if previous is None and content is None:
                raise FileNotFoundError(path)
            # This path is protected by the existing child-process filesystem fence.
            before = linked_file(self.service, record, path) if previous else None
            if before is not None:
                source_resource(store, self.resources, record, path, linked_path=before)
            if record.linked_access == "publish_later":
                from clio_agent.gact.storage.linked_changes import stage_edit

                updated = stage_edit(self.service, record, path, content, before)
                self.revision = updated.linked_manifest_id or ""
                return
            with linked_adapter(self.service, record, refresh=True) as adapter:
                current = next((row for row in adapter.entries() if row.path == path), None)
                if bool(current) != bool(previous):
                    raise ValueError("The source changed; refresh the folder before writing")
                if current is not None:
                    digest = hashlib.sha256()
                    total_read = 0
                    with adapter.open_read(current) as reader:
                        while chunk := reader.read(1024 * 1024):
                            total_read += len(chunk)
                            if total_read > MAX_WRITE:
                                raise ValueError("This file exceeds the linked-write limit")
                            digest.update(chunk)
                    if before is None or digest.hexdigest() != file_hash(before):
                        raise ValueError("The source changed; refresh the folder before writing")
                operation = store.begin_operation(self.source_id, "apply")
                self.last_operation = operation.id
                store.update_operation(operation.id, state="running", selected_paths=[path])
                stage: Path | None = None
                try:
                    if content is not None:
                        owner = store.root / self.source_id
                        owner.mkdir(exist_ok=True)
                        stage = owner / ("write-" + operation.id)
                        with stage.open("xb") as writer:
                            total = 0
                            while chunk := content.read(1024 * 1024):
                                total += len(chunk)
                                if total > MAX_WRITE:
                                    raise ValueError("This file exceeds the linked-write limit")
                                writer.write(chunk)
                        os.chmod(stage, 0o400)
                    self._record(write=True)
                    adapter.apply(path, stage, current.revision if current else None)
                    store.update_operation(operation.id, state="completed", applied_paths=[path])
                except Exception as exc:
                    store.update_operation(operation.id, state="failed", error=str(exc))
                    raise
                finally:
                    if stage is not None and stage.exists():
                        os.chmod(stage, 0o600)
                        stage.unlink()
            # A new index never overwrites the previous immutable index or preimage.
            updated = link_folder(self.service, record)
            self.revision = updated.linked_manifest_id or ""
            if content is not None:
                after = linked_file(self.service, updated, path)
                source_resource(store, self.resources, updated, path, linked_path=after)
