"""Workspace-scoped custody for user-uploaded resources.

The resource store owns original bytes and their identity independently of the
workspace file tree and artifact registry. Uploads are resumable by byte offset;
the server computes content identity and MIME evidence before a resource becomes
available to messages, previews, or tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from clio_agent.gact.resource_mime import detect_media_type

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    """Return a display-only filename with path components removed."""

    normalized = value.replace("\\", "/").strip()
    name = normalized.rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        raise ValueError("resource name must identify one file")
    if any(ord(character) < 32 for character in name):
        raise ValueError("resource name contains control characters")
    return name[:255]


class ResourceRecord(BaseModel):
    """Durable metadata for one immutable resource revision."""

    id: str
    workspace_id: str
    client_upload_id: str = ""
    revision: int = 1
    name: str
    claimed_mime: str = ""
    detected_mime: str = ""
    detection_source: str = ""
    declared_size: int
    received_size: int = 0
    sha256: str = ""
    state: Literal["uploading", "ready", "quarantined", "failed"] = "uploading"
    failure: str = ""
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)
    completed_at: str = ""

    @property
    def mime_mismatch(self) -> bool:
        return bool(
            self.claimed_mime
            and self.detected_mime
            and self.claimed_mime.lower() != self.detected_mime.lower()
        )

    def to_wire(self) -> dict[str, object]:
        row = self.model_dump()
        row["mime_mismatch"] = self.mime_mismatch
        return row


class ResourceConflictError(ValueError):
    """Raised when an upload offset or resource state is stale."""

    def __init__(self, message: str, record: ResourceRecord) -> None:
        super().__init__(message)
        self.record = record


class ResourceLimitError(ValueError):
    """Raised when an upload exceeds a deployment boundary."""


class ResourceDeleteError(RuntimeError):
    """Raised when a resource's bytes could not be removed.

    The index record is deliberately KEPT when this is raised: dropping it
    first (as the original delete order did) loses the resource from memory
    while its bytes survive on disk, so memory and disk disagree until the next
    restart re-reads the index.
    """

    def __init__(self, message: str, record: ResourceRecord, reason: str) -> None:
        super().__init__(message)
        self.record = record
        self.reason = reason


def quarantine_corrupt_index(path: Path, exc: BaseException, *, kind: str) -> dict[str, str]:
    """Move an unreadable index aside and return the typed degradation reason.

    A composer index that fails to parse used to raise out of ``build_app``, so
    ONE corrupt JSON file took down the whole server — no sessions at all. The
    graceful-degradation chain instead quarantines the file (it is evidence, so
    it is renamed rather than deleted), starts from an empty store, and reports
    WHY through this typed reason so the loss is never silent.
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    quarantined = path.with_name(f"{path.name}.corrupt-{stamp}")
    reason = {
        "reason": "composer_index_unreadable",
        "kind": kind,
        "path": str(path),
        "quarantined_path": str(quarantined),
        "error": type(exc).__name__,
        "detail": str(exc),
    }
    try:
        os.replace(path, quarantined)
    except OSError as move_error:
        reason["quarantined_path"] = ""
        reason["quarantine_error"] = type(move_error).__name__
    logger.warning(
        "composer index unreadable reason=composer_index_unreadable kind=%s path=%s "
        "quarantined=%s error=%s",
        kind,
        path,
        reason["quarantined_path"],
        type(exc).__name__,
    )
    return reason


class ResourceStore:
    """Thread-safe original-byte custody with atomic metadata persistence."""

    def __init__(self, *, root: Path, max_resource_bytes: int) -> None:
        self.root = root
        self.max_resource_bytes = max_resource_bytes
        self._index_path = root / "resources.json"
        self._lock = threading.RLock()
        self._records: dict[str, ResourceRecord] = {}
        self.load_degradation: dict[str, str] | None = None
        self._load()

    def _load(self) -> None:
        if not self._index_path.exists():
            return
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
            loaded: dict[str, ResourceRecord] = {}
            for row in payload.get("resources", []):
                record = ResourceRecord(**row)
                if record.state == "uploading":
                    upload = self._upload_path(record)
                    record.received_size = upload.stat().st_size if upload.exists() else 0
                loaded[record.id] = record
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.load_degradation = quarantine_corrupt_index(
                self._index_path, exc, kind="resource_custody_index"
            )
            self._records = {}
            return
        self._records = loaded

    def _flush_locked(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self._index_path.with_suffix(".json.tmp")
        payload = {"resources": [row.model_dump() for row in self._records.values()]}
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp, self._index_path)

    def _revision_dir(self, record: ResourceRecord) -> Path:
        return self.root / record.workspace_id / record.id / f"v{record.revision}"

    def _upload_path(self, record: ResourceRecord) -> Path:
        return self._revision_dir(record) / "original.upload"

    def content_path(self, record: ResourceRecord) -> Path:
        """Return the private original-byte path for a ready resource."""

        if record.state != "ready":
            raise ResourceConflictError("resource content is not ready", record)
        return self._revision_dir(record) / "original"

    def create_or_resume(
        self,
        *,
        workspace_id: str,
        name: str,
        declared_size: int,
        claimed_mime: str = "",
        client_upload_id: str = "",
    ) -> tuple[ResourceRecord, bool]:
        """Create an upload or resume the record for a stable client upload key."""

        if declared_size < 0:
            raise ValueError("declared_size must be non-negative")
        if declared_size > self.max_resource_bytes:
            raise ResourceLimitError(
                f"resource exceeds deployment limit of {self.max_resource_bytes} bytes"
            )
        safe_name = _safe_name(name)
        normalized_mime = claimed_mime.strip().lower()
        normalized_upload_id = client_upload_id.strip()
        if len(normalized_upload_id) > 200 or any(
            ord(character) < 32 for character in normalized_upload_id
        ):
            raise ValueError("client_upload_id is invalid")
        with self._lock:
            if normalized_upload_id:
                existing = next(
                    (
                        row
                        for row in self._records.values()
                        if row.workspace_id == workspace_id
                        and row.client_upload_id == normalized_upload_id
                    ),
                    None,
                )
                if existing is not None:
                    if (
                        existing.name != safe_name
                        or existing.declared_size != declared_size
                        or existing.claimed_mime != normalized_mime
                    ):
                        raise ResourceConflictError(
                            "client upload identity is already bound to different metadata",
                            existing,
                        )
                    return existing.model_copy(deep=True), True

            record = ResourceRecord(
                id="res_" + uuid.uuid4().hex,
                workspace_id=workspace_id,
                client_upload_id=normalized_upload_id,
                name=safe_name,
                declared_size=declared_size,
                claimed_mime=normalized_mime,
            )
            revision_dir = self._revision_dir(record)
            revision_dir.mkdir(parents=True, exist_ok=False)
            self._upload_path(record).touch(exist_ok=False)
            self._records[record.id] = record
            if declared_size == 0:
                record = self._finalize_locked(record)
            self._flush_locked()
            return record.model_copy(deep=True), False

    def append(self, resource_id: str, *, offset: int, data: bytes) -> ResourceRecord:
        """Append a bounded upload chunk at the caller's authoritative offset."""

        with self._lock:
            record = self._require_locked(resource_id)
            if record.state != "uploading":
                raise ResourceConflictError("resource upload is already complete", record)
            if offset != record.received_size:
                raise ResourceConflictError("upload offset does not match server state", record)
            new_size = offset + len(data)
            if new_size > record.declared_size or new_size > self.max_resource_bytes:
                raise ResourceLimitError("upload chunk exceeds the declared resource size")
            with self._upload_path(record).open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            record.received_size = new_size
            record.updated_at = _now_iso()
            if new_size == record.declared_size:
                record = self._finalize_locked(record)
            self._records[record.id] = record
            self._flush_locked()
            return record.model_copy(deep=True)

    def _finalize_locked(self, record: ResourceRecord) -> ResourceRecord:
        upload = self._upload_path(record)
        digest = hashlib.sha256()
        head = b""
        with upload.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if len(head) < 8192:
                    head += chunk[: 8192 - len(head)]
                digest.update(chunk)
        detected_mime, detection_source = detect_media_type(record.name, head)
        destination = self._revision_dir(record) / "original"
        os.replace(upload, destination)
        now = _now_iso()
        return record.model_copy(
            update={
                "sha256": digest.hexdigest(),
                "detected_mime": detected_mime,
                "detection_source": detection_source,
                "state": "ready",
                "completed_at": now,
                "updated_at": now,
            }
        )

    def get(self, workspace_id: str, resource_id: str) -> ResourceRecord | None:
        with self._lock:
            record = self._records.get(resource_id)
            if record is None or record.workspace_id != workspace_id:
                return None
            return record.model_copy(deep=True)

    def list(self, workspace_id: str) -> list[ResourceRecord]:
        with self._lock:
            return sorted(
                (
                    row.model_copy(deep=True)
                    for row in self._records.values()
                    if row.workspace_id == workspace_id
                ),
                key=lambda row: row.created_at,
                reverse=True,
            )

    def delete(self, workspace_id: str, resource_id: str) -> bool:
        """Delete the original, upload residue, derivatives, and index record.

        BYTES FIRST, then the record. A concurrent reader holding the original
        open makes ``rmtree`` raise ``PermissionError`` on Windows; popping the
        record first would have lost it from memory (with no flush) while the
        bytes survived on disk, so memory and disk disagreed until a restart.
        Failing before the pop keeps the record authoritative and surfaces the
        error typed.

        Raises:
            ResourceDeleteError: The bytes could not be removed. The index
                record is unchanged and the resource stays listed.
        """

        with self._lock:
            record = self._records.get(resource_id)
            if record is None or record.workspace_id != workspace_id:
                return False
            resource_root = self.root / workspace_id / resource_id
            if resource_root.exists():
                try:
                    shutil.rmtree(resource_root)
                except OSError as exc:
                    raise ResourceDeleteError(
                        f"resource bytes could not be removed: {exc}",
                        record.model_copy(deep=True),
                        type(exc).__name__,
                    ) from exc
            self._records.pop(resource_id, None)
            workspace_root = self.root / workspace_id
            if workspace_root.exists() and not any(workspace_root.iterdir()):
                workspace_root.rmdir()
            self._flush_locked()
            return True

    def delete_workspace(self, workspace_id: str) -> int:
        """Delete every resource owned by a deleted workspace.

        Same ordering rule as :meth:`delete`: the bytes go first, so a failed
        removal leaves the index records intact and the error visible to the
        caller rather than dropping every record while the tree survives.
        """

        with self._lock:
            ids = [
                resource_id
                for resource_id, record in self._records.items()
                if record.workspace_id == workspace_id
            ]
            workspace_root = self.root / workspace_id
            if workspace_root.exists():
                shutil.rmtree(workspace_root)
            for resource_id in ids:
                self._records.pop(resource_id, None)
            self._flush_locked()
            return len(ids)

    def _require_locked(self, resource_id: str) -> ResourceRecord:
        record = self._records.get(resource_id)
        if record is None:
            raise KeyError(resource_id)
        return record


__all__ = [
    "ResourceConflictError",
    "ResourceDeleteError",
    "ResourceLimitError",
    "ResourceRecord",
    "ResourceStore",
    "quarantine_corrupt_index",
]
