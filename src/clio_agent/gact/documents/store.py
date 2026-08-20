"""Durable document reviews and confined, watched working copies."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import paths
from clio_agent.gact.artifacts.cas import CASStore, ingest_identity, sha256_file
from clio_agent.gact.artifacts.minting import mint_artifact_outcome
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion, Custody, Mechanism
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.documents.models import (
    ArtifactReview,
    DocumentWorkingCopy,
    EditorProvider,
)
from clio_agent.gact.documents.native_comments import (
    UnsafeDocumentArchiveError,
    extract_native_comments,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_POLL_SECONDS = 0.5
_STABLE_SECONDS = 1.5
_HASH_CHUNK_BYTES = 1024 * 1024


class DocumentStoreError(RuntimeError):
    """Base class for typed document-store failures."""


class WorkingCopyLeaseError(DocumentStoreError):
    """A writable working copy already holds the artifact lease."""


class WorkingCopyConflictError(DocumentStoreError):
    """A working copy save is based on a stale artifact head."""


class DocumentIntegrityError(DocumentStoreError):
    """Artifact bytes do not match their immutable identity."""


CheckpointCallback = Callable[[DocumentWorkingCopy, list[ArtifactReview]], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _safe_filename(name: str) -> str:
    filename = Path(name).name.strip()
    if not filename or filename in {".", ".."}:
        return "document.bin"
    return filename


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class DocumentStore:
    """Per-app durable review ledger and stable-save working-copy monitor."""

    def __init__(self, app: "FastAPI") -> None:
        self._app = app
        self._lock = threading.RLock()
        self._reviews: dict[str, ArtifactReview] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._working_copies: dict[str, DocumentWorkingCopy] = {}
        self._loaded_workspaces: set[str] = set()
        self._pending_signatures: dict[str, tuple[tuple[int, int], float]] = {}
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._checkpoint_callback: CheckpointCallback | None = None

    def set_checkpoint_callback(self, callback: CheckpointCallback) -> None:
        """Register the route-owned agent dispatch callback."""

        self._checkpoint_callback = callback

    def close(self) -> None:
        """Stop the background save monitor."""

        self._stop.set()
        monitor = self._monitor
        if monitor is not None and monitor.is_alive():
            monitor.join(timeout=3.0)

    def create_review(self, review: ArtifactReview) -> ArtifactReview:
        """Persist a review, returning an idempotent prior result when present."""

        with self._lock:
            self._load_workspace(review.workspace_id)
            key = (review.session_id, review.idempotency_key)
            prior_id = self._idempotency.get(key) if review.idempotency_key else None
            if prior_id:
                return self._reviews[prior_id]
            self._reviews[review.id] = review
            if review.idempotency_key:
                self._idempotency[key] = review.id
            self._append_review(review)
            return review

    def update_review(self, review_id: str, **changes: Any) -> ArtifactReview:
        """Append a new projection row for a review state change."""

        with self._lock:
            current = self._reviews.get(review_id)
            if current is None:
                raise KeyError(review_id)
            updated = current.model_copy(update=changes)
            self._reviews[review_id] = updated
            self._append_review(updated)
            return updated

    def list_reviews(self, workspace_id: str, artifact_name: str) -> list[ArtifactReview]:
        """List the latest projection of every review in a logical artifact chain."""

        with self._lock:
            self._load_workspace(workspace_id)
            rows = [
                review
                for review in self._reviews.values()
                if review.workspace_id == workspace_id and review.artifact_name == artifact_name
            ]
        return sorted(rows, key=lambda review: review.created_at)

    def create_working_copy(
        self,
        *,
        session_id: str,
        workspace_id: str,
        record: ArtifactRecord,
        version: ArtifactVersion,
        provider: EditorProvider,
        writable: bool,
        auto_checkpoint: bool,
    ) -> DocumentWorkingCopy:
        """Materialize an exact immutable version under a confined working-copy root."""

        with self._lock:
            self._load_workspace(workspace_id)
            if writable:
                for existing in self._working_copies.values():
                    if (
                        existing.workspace_id == workspace_id
                        and existing.artifact_name == record.name
                        and existing.writable
                        and existing.status == "active"
                    ):
                        if (
                            existing.session_id == session_id
                            and existing.head_artifact_id == version.artifact_id
                        ):
                            return existing
                        raise WorkingCopyLeaseError(
                            f"artifact {record.name!r} already has writable working copy "
                            f"{existing.id}"
                        )
            working_copy_id = _new_id("docwc")
            root = self._workspace_root(workspace_id)
            workdir = self._documents_root(root) / "working-copies" / working_copy_id
            workdir.mkdir(parents=True, exist_ok=False)
            target = workdir / _safe_filename(record.name)
            self._copy_verified_version(root, version, target)
            now = _now_iso()
            fingerprints = self._native_comment_fingerprints(target)
            row = DocumentWorkingCopy(
                id=working_copy_id,
                session_id=session_id,
                workspace_id=workspace_id,
                artifact_name=record.name,
                base_artifact_id=version.artifact_id,
                head_artifact_id=version.artifact_id,
                base_version=version.version,
                head_version=version.version,
                base_sha256=version.sha256 or sha256_file(target),
                last_sha256=version.sha256 or sha256_file(target),
                path=str(target.resolve()),
                provider=provider,
                writable=writable,
                auto_checkpoint=auto_checkpoint,
                status="active",
                created_at=now,
                updated_at=now,
                native_comment_fingerprints=sorted(fingerprints),
            )
            self._working_copies[row.id] = row
            self._persist_working_copy(row)
            if writable and auto_checkpoint:
                self._start_monitor()
            return row

    def get_working_copy(self, working_copy_id: str) -> DocumentWorkingCopy | None:
        """Resolve a working copy across the app's registered workspaces."""

        with self._lock:
            found = self._working_copies.get(working_copy_id)
            if found is not None:
                return found
            for workspace_id in self._workspace_ids():
                self._load_workspace(workspace_id)
                found = self._working_copies.get(working_copy_id)
                if found is not None:
                    return found
        return None

    def close_working_copy(self, working_copy_id: str) -> DocumentWorkingCopy:
        """Close a working-copy lease while preserving its on-disk file."""

        with self._lock:
            current = self._require_working_copy(working_copy_id)
            updated = current.model_copy(update={"status": "closed", "updated_at": _now_iso()})
            self._working_copies[working_copy_id] = updated
            self._persist_working_copy(updated)
            self._pending_signatures.pop(working_copy_id, None)
            return updated

    def checkpoint(
        self, working_copy_id: str, *, allow_stale_head: bool = False
    ) -> DocumentWorkingCopy:
        """Hash a stable save and mint its next immutable artifact version."""

        with self._lock:
            current = self._require_working_copy(working_copy_id)
            if current.status == "closed":
                return current
            path = Path(current.path)
            if not path.is_file():
                updated = current.model_copy(
                    update={"status": "missing", "updated_at": _now_iso(), "error": "file missing"}
                )
                self._replace_working_copy(updated)
                return updated
            try:
                current_sha = _sha256_stable(path)
            except OSError as exc:
                updated = current.model_copy(
                    update={"status": "error", "updated_at": _now_iso(), "error": str(exc)}
                )
                self._replace_working_copy(updated)
                return updated
            if current_sha == current.last_sha256:
                return current
            registry = get_registry(self._app)
            record = registry.get(current.workspace_id, current.artifact_name)
            if record is None or record.head is None:
                updated = current.model_copy(
                    update={
                        "status": "error",
                        "updated_at": _now_iso(),
                        "error": "artifact chain no longer exists",
                    }
                )
                self._replace_working_copy(updated)
                return updated
            if record.head.artifact_id != current.head_artifact_id and not allow_stale_head:
                updated = current.model_copy(
                    update={
                        "status": "conflict",
                        "updated_at": _now_iso(),
                        "conflict_head_artifact_id": record.head.artifact_id,
                        "conflict_candidate_sha256": current_sha,
                        "error": "artifact head advanced while this working copy was open",
                    }
                )
                self._replace_working_copy(updated)
                self._emit(
                    current.session_id,
                    "document.working_copy.conflict",
                    updated,
                    status="failed",
                )
                return updated
            root = self._workspace_root(current.workspace_id)
            ingested = ingest_identity(path, workspace_root=root)
            outcome = mint_artifact_outcome(
                self._app,
                current.session_id,
                name=current.artifact_name,
                workspace_id=current.workspace_id,
                evidence=ingested.evidence,
                kind=record.kind,
                mechanism=Mechanism.CHANGE_FEED,
                producer={
                    "designation": "document-working-copy-save",
                    "session_id": current.session_id,
                    "working_copy_id": current.id,
                },
                custody=ingested.custody,
                path=str(path),
                turn_id=f"document-save:{current.id}",
                not_ingested_size=ingested.not_ingested_size,
            )
            if outcome is None:
                raise DocumentStoreError("artifact mint returned no outcome")
            version = outcome.version
            comments = self._ingest_new_native_comments(current, path, version)
            updated = current.model_copy(
                update={
                    "head_artifact_id": version.artifact_id,
                    "head_version": version.version,
                    "last_sha256": current_sha,
                    "status": "active",
                    "updated_at": _now_iso(),
                    "last_checkpoint_at": _now_iso(),
                    "conflict_head_artifact_id": "",
                    "conflict_candidate_sha256": "",
                    "error": "",
                    "native_comment_fingerprints": sorted(self._native_comment_fingerprints(path)),
                }
            )
            self._replace_working_copy(updated)
            self._emit(current.session_id, "document.working_copy.changed", updated)
            callback = self._checkpoint_callback
        if callback is not None:
            callback(updated, comments)
        return updated

    def resolve_conflict(
        self,
        working_copy_id: str,
        *,
        resolution: str,
        expected_head_artifact_id: str,
    ) -> DocumentWorkingCopy:
        """Resolve a working-copy conflict through an explicit user choice."""

        with self._lock:
            current = self._require_working_copy(working_copy_id)
            registry = get_registry(self._app)
            record = registry.get(current.workspace_id, current.artifact_name)
            if record is None or record.head is None:
                raise DocumentStoreError("artifact chain no longer exists")
            if record.head.artifact_id != expected_head_artifact_id:
                raise WorkingCopyConflictError("artifact head changed during conflict resolution")
            if resolution == "use-working-copy":
                rebased = current.model_copy(
                    update={
                        "head_artifact_id": record.head.artifact_id,
                        "head_version": record.head.version,
                        "status": "active",
                        "error": "",
                    }
                )
                self._replace_working_copy(rebased)
            elif resolution == "keep-current":
                target = Path(current.path)
                self._copy_verified_version(
                    self._workspace_root(current.workspace_id),
                    record.head,
                    target,
                )
                rebased = current.model_copy(
                    update={
                        "head_artifact_id": record.head.artifact_id,
                        "head_version": record.head.version,
                        "last_sha256": record.head.sha256 or sha256_file(target),
                        "status": "active",
                        "updated_at": _now_iso(),
                        "conflict_head_artifact_id": "",
                        "conflict_candidate_sha256": "",
                        "error": "",
                    }
                )
                self._replace_working_copy(rebased)
                return rebased
            else:
                raise ValueError(f"unknown conflict resolution: {resolution}")
        return self.checkpoint(working_copy_id, allow_stale_head=True)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(_POLL_SECONDS):
            with self._lock:
                rows = [
                    row
                    for row in self._working_copies.values()
                    if row.status == "active" and row.writable and row.auto_checkpoint
                ]
            for row in rows:
                path = Path(row.path)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                signature = (int(stat.st_size), int(stat.st_mtime_ns))
                pending = self._pending_signatures.get(row.id)
                if pending is None or pending[0] != signature:
                    self._pending_signatures[row.id] = (signature, time.monotonic())
                    continue
                if time.monotonic() - pending[1] < _STABLE_SECONDS:
                    continue
                self._pending_signatures.pop(row.id, None)
                self.checkpoint(row.id)

    def _start_monitor(self) -> None:
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._stop.clear()
        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name="clio-document-working-copies",
            daemon=True,
        )
        self._monitor.start()

    def _copy_verified_version(
        self, workspace_root: Path, version: ArtifactVersion, target: Path
    ) -> None:
        source: Path | None = None
        if version.custody == Custody.CAS and version.sha256:
            candidate = CASStore(workspace_root).blob_path(version.sha256)
            if candidate.is_file():
                source = candidate
        if source is None and version.path:
            candidate = Path(version.path)
            if candidate.is_file():
                source = candidate
        if source is None:
            raise DocumentStoreError("artifact version bytes are unavailable")
        if version.sha256 and sha256_file(source) != version.sha256:
            raise DocumentIntegrityError("artifact bytes do not match their recorded hash")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)

    def _ingest_new_native_comments(
        self,
        working_copy: DocumentWorkingCopy,
        path: Path,
        version: ArtifactVersion,
    ) -> list[ArtifactReview]:
        known = set(working_copy.native_comment_fingerprints)
        try:
            comments = extract_native_comments(path)
        except (OSError, zipfile.BadZipFile, UnsafeDocumentArchiveError) as exc:
            logger.warning(
                "document native comments skipped reason=parse_failed working_copy=%s error=%s",
                working_copy.id,
                exc,
            )
            return []
        created: list[ArtifactReview] = []
        for comment in comments:
            if comment.fingerprint in known:
                continue
            agent_bound = comment.text.lstrip().lower().startswith("@clio")
            review = ArtifactReview(
                id=_new_id("docreview"),
                session_id=working_copy.session_id,
                workspace_id=working_copy.workspace_id,
                artifact_id=version.artifact_id,
                artifact_name=working_copy.artifact_name,
                artifact_version=version.version,
                artifact_sha256=version.sha256 or sha256_file(path),
                anchor=comment.anchor,
                text=comment.text,
                status="queued" if agent_bound else "human-note",
                native=True,
                native_text_hash=comment.fingerprint,
                idempotency_key=f"native:{working_copy.id}:{comment.fingerprint}",
                created_at=_now_iso(),
            )
            created.append(self.create_review(review))
            self._emit(
                working_copy.session_id,
                "document.native_comment.imported",
                working_copy,
                extra={"review_id": review.id, "agent_bound": agent_bound},
            )
        return created

    def _native_comment_fingerprints(self, path: Path) -> set[str]:
        try:
            return {comment.fingerprint for comment in extract_native_comments(path)}
        except (OSError, zipfile.BadZipFile, UnsafeDocumentArchiveError):
            return set()

    def _emit(
        self,
        session_id: str,
        event_type: str,
        working_copy: DocumentWorkingCopy,
        *,
        status: str = "completed",
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        from clio_agent.gact.runtime.globals import _emit_semantic_event

        payload: dict[str, Any] = {
            "working_copy_id": working_copy.id,
            "workspace_id": working_copy.workspace_id,
            "artifact_name": working_copy.artifact_name,
            "head_artifact_id": working_copy.head_artifact_id,
            "head_version": working_copy.head_version,
            "status": working_copy.status,
        }
        payload.update(extra or {})
        _emit_semantic_event(
            self._app,
            session_id,
            event_type,
            status=status,
            summary=f"Document working copy {working_copy.id} {working_copy.status}.",
            actor={"role": "user"},
            subject={
                "working_copy_id": working_copy.id,
                "artifact_name": working_copy.artifact_name,
            },
            payload=payload,
            detail_level="semantic",
        )

    def _append_review(self, review: ArtifactReview) -> None:
        root = self._workspace_root(review.workspace_id)
        ledger = self._documents_root(root) / "reviews.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(review.model_dump_json())
            handle.write("\n")

    def _load_workspace(self, workspace_id: str) -> None:
        if workspace_id in self._loaded_workspaces:
            return
        root = self._workspace_root(workspace_id)
        documents_root = self._documents_root(root)
        ledger = documents_root / "reviews.jsonl"
        if ledger.is_file():
            for raw in ledger.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                review = ArtifactReview.model_validate_json(raw)
                self._reviews[review.id] = review
                if review.idempotency_key:
                    self._idempotency[(review.session_id, review.idempotency_key)] = review.id
        copies_root = documents_root / "working-copies"
        if copies_root.is_dir():
            for manifest in copies_root.glob("*/manifest.json"):
                try:
                    row = DocumentWorkingCopy.model_validate_json(
                        manifest.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                self._working_copies[row.id] = row
        self._loaded_workspaces.add(workspace_id)
        if any(
            row.workspace_id == workspace_id
            and row.status == "active"
            and row.writable
            and row.auto_checkpoint
            for row in self._working_copies.values()
        ):
            self._start_monitor()

    def _persist_working_copy(self, row: DocumentWorkingCopy) -> None:
        path = Path(row.path).parent / "manifest.json"
        _atomic_json(path, row.model_dump(mode="json"))

    def _replace_working_copy(self, row: DocumentWorkingCopy) -> None:
        self._working_copies[row.id] = row
        self._persist_working_copy(row)

    def _require_working_copy(self, working_copy_id: str) -> DocumentWorkingCopy:
        row = self.get_working_copy(working_copy_id)
        if row is None:
            raise KeyError(working_copy_id)
        return row

    def _workspace_root(self, workspace_id: str) -> Path:
        store = getattr(self._app.state, "workspaces", None)
        workspace = store.get(workspace_id) if store is not None else None
        root = str(getattr(workspace, "root_path", "") or "") if workspace is not None else ""
        if not root:
            raise DocumentStoreError(f"workspace root is unavailable: {workspace_id}")
        return Path(root).expanduser().resolve(strict=False)

    def _workspace_ids(self) -> list[str]:
        store = getattr(self._app.state, "workspaces", None)
        if store is None:
            return []
        rows: list[Any] = getattr(store, "list", lambda: [])()
        return [str(getattr(row, "id", "") or "") for row in rows if getattr(row, "id", "")]

    @staticmethod
    def _documents_root(workspace_root: Path) -> Path:
        return paths.workspace_agent_dir(workspace_root) / "documents"


def _sha256_stable(path: Path) -> str:
    """Hash one stable file using a same-handle stat check."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise OSError("file changed while it was being checkpointed")
    return digest.hexdigest()


def get_document_store(app: "FastAPI") -> DocumentStore:
    """Return the app-scoped document store, creating it lazily."""

    existing = getattr(app.state, "document_store", None)
    if isinstance(existing, DocumentStore):
        return existing
    created = DocumentStore(app)
    app.state.document_store = created
    return created


__all__ = [
    "DocumentIntegrityError",
    "DocumentStore",
    "DocumentStoreError",
    "WorkingCopyConflictError",
    "WorkingCopyLeaseError",
    "get_document_store",
]
