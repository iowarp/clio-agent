"""Extensible structured conversion for workspace-owned resources.

The custody layer deliberately knows nothing about Docling.  Converters are
registered here with an explicit priority and selected from the resource's
server-detected MIME type.  The CLIO web-search Docling service is the first
built-in implementation, not a hard-coded processing path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field

from clio_agent import conf
from clio_agent.gact.resource_custody import ResourceRecord, ResourceStore

_SAFE_DERIVATIVE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_NODE_BYTES = 2 * 1024 * 1024
_ALLOWED_NODE_COLLECTIONS = {"pages", "tables", "pictures", "texts"}

# Floor throughput used to DERIVE the processor's write timeout from
# ``resources.max_bytes`` when the operator has not pinned one: an upload of the
# largest permitted resource must not be cut off mid-body just because the
# ceiling was raised. 1 MiB/s is deliberately pessimistic (a slow shared link);
# raise ``resources.processor_write_timeout_s`` to pin a value instead.
_PROCESSOR_MIN_UPLOAD_BYTES_PER_S = 1024 * 1024
_PROCESSOR_WRITE_TIMEOUT_FLOOR_S = 60.0

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derivative_name_max_chars() -> int:
    return max(
        16,
        conf.resolve(
            "resources.derivative_name_max_chars",
            env="CLIO_RESOURCE_DERIVATIVE_NAME_MAX_CHARS",
            default=48,
            cast=conf.as_int,
        ),
    )


def _derivative_filename(derivative_id: str) -> str:
    """Return a length-bounded on-disk name for a derivative id.

    Derivative ids are accepted up to 128 characters, and custody nests them
    under ``<root>/<workspace>/<resource>/v<rev>/processing/derivatives/`` — on
    Windows that can cross the 260-character path limit and fail the write. Ids
    past the bound are stored under a digest name; the manifest keeps the real
    id, so lookups are unaffected.
    """

    bound = _derivative_name_max_chars()
    if len(derivative_id) <= bound:
        return derivative_id
    return hashlib.sha256(derivative_id.encode("utf-8")).hexdigest()[:32]


class ResourceCustodyGone(RuntimeError):
    """Raised when processing state is written for a resource that was deleted."""


class ResourceProcessingRecord(BaseModel):
    """Durable state for one resource revision's processing job."""

    workspace_id: str
    resource_id: str
    resource_revision: int
    source_sha256: str
    processor: str = ""
    processor_url: str = ""
    job_id: str = ""
    state: Literal["not_started", "submitted", "processing", "complete", "failed", "cancelled"] = (
        "not_started"
    )
    progress: int = 0
    # Consecutive failed status polls. Reset on any answered poll; past
    # ``resources.status_poll_failure_threshold`` the record degrades to a typed
    # ``converter_status_unavailable`` failure instead of waiting forever.
    poll_failures: int = 0
    derivatives_available: bool = False
    failure: dict[str, Any] = Field(default_factory=dict)
    cancellation: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class ResourceProcessingStore:
    """Persist processor state and named derivatives beside the original bytes."""

    def __init__(self, resources: ResourceStore) -> None:
        self.resources = resources
        self._lock = threading.RLock()

    def _root(self, record: ResourceRecord) -> Path:
        return (
            self.resources.root
            / record.workspace_id
            / record.id
            / f"v{record.revision}"
            / "processing"
        )

    def _state_path(self, record: ResourceRecord) -> Path:
        return self._root(record) / "processing.json"

    def state(self, record: ResourceRecord) -> ResourceProcessingRecord:
        path = self._state_path(record)
        with self._lock:
            if not path.exists():
                return ResourceProcessingRecord(
                    workspace_id=record.workspace_id,
                    resource_id=record.id,
                    resource_revision=record.revision,
                    source_sha256=record.sha256,
                )
            state = ResourceProcessingRecord(**json.loads(path.read_text(encoding="utf-8")))
            if not state.derivatives_available and (self._root(record) / "manifest.json").exists():
                state = state.model_copy(update={"derivatives_available": True})
            return state

    def _require_custody(self, record: ResourceRecord) -> Path:
        """Return the processing root, refusing once the resource is deleted.

        ``mkdir(parents=True)`` would otherwise RE-CREATE a custody tree that a
        concurrent DELETE just removed, resurrecting a ghost resource on disk
        and letting a background submit keep publishing lifecycle events for it.
        """

        revision_root = (
            self.resources.root / record.workspace_id / record.id / f"v{record.revision}"
        )
        if not revision_root.is_dir():
            raise ResourceCustodyGone(
                f"resource custody is gone: {record.workspace_id}/{record.id}"
            )
        return revision_root / "processing"

    def save_state(self, record: ResourceRecord, state: ResourceProcessingRecord) -> None:
        with self._lock:
            root = self._require_custody(record)
            root.mkdir(parents=True, exist_ok=True)
            path = self._state_path(record)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(state.model_dump(), indent=2), encoding="utf-8")
            os.replace(temp, path)

    def save_result(
        self,
        record: ResourceRecord,
        state: ResourceProcessingRecord,
        result: dict[str, Any],
    ) -> ResourceProcessingRecord:
        """Persist canonical structure and safe named derivative content."""

        document = result.get("document")
        if not isinstance(document, dict) or not isinstance(document.get("structure"), dict):
            raise ValueError("processor result did not contain canonical document structure")
        manifest = result.get("derivatives")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
            raise ValueError("processor result did not contain a derivative manifest")
        with self._lock:
            root = self._require_custody(record)
            derivatives_root = root / "derivatives"
            derivatives_root.mkdir(parents=True, exist_ok=True)
            (root / "structure.json").write_text(
                json.dumps(document["structure"], ensure_ascii=False),
                encoding="utf-8",
            )
            persisted_entries: list[dict[str, Any]] = []
            for raw in manifest["entries"]:
                if not isinstance(raw, dict):
                    continue
                entry = dict(raw)
                derivative_id = str(entry.get("id") or "")
                if not _SAFE_DERIVATIVE_ID.fullmatch(derivative_id):
                    raise ValueError("processor returned an invalid derivative id")
                content = entry.pop("content", None)
                if isinstance(content, str):
                    destination = derivatives_root / _derivative_filename(derivative_id)
                    destination.write_text(content, encoding="utf-8")
                    entry["content_path"] = destination.name
                    entry["size"] = destination.stat().st_size
                persisted_entries.append(entry)
            saved_manifest = {
                # Everything the processor said about the manifest as a WHOLE is
                # carried through (``entries_truncated`` / ``entry_counts``), so
                # a partial derivative list stays visible to enrichment instead
                # of being silently flattened to "here are the derivatives".
                **{
                    key: value
                    for key, value in manifest.items()
                    if key not in {"schema", "entries"}
                },
                "schema": str(manifest.get("schema") or "clio.resource-derivatives.v1"),
                "source": {
                    "resource_id": record.id,
                    "revision": record.revision,
                    "sha256": record.sha256,
                },
                "document": {key: value for key, value in document.items() if key != "structure"},
                "entries": persisted_entries,
            }
            (root / "manifest.json").write_text(
                json.dumps(saved_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            completed = state.model_copy(
                update={
                    "state": "complete",
                    "progress": 100,
                    "derivatives_available": True,
                    "updated_at": _now_iso(),
                }
            )
            self.save_state(record, completed)
            return completed

    def manifest(self, record: ResourceRecord) -> dict[str, Any] | None:
        path = self._root(record) / "manifest.json"
        with self._lock:
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def structure_outline(self, record: ResourceRecord) -> dict[str, Any]:
        structure = self._load_structure(record)
        return {
            "resource_id": record.id,
            "revision": record.revision,
            "collections": {
                key: len(value) if isinstance(value, list | dict) else 0
                for key, value in structure.items()
                if key in _ALLOWED_NODE_COLLECTIONS
            },
        }

    def node(self, record: ResourceRecord, collection: str, index: int) -> Any:
        if collection not in _ALLOWED_NODE_COLLECTIONS:
            raise KeyError(collection)
        structure = self._load_structure(record)
        values = structure.get(collection)
        if isinstance(values, dict):
            keys = list(values)
            if index < 0 or index >= len(keys):
                raise IndexError(index)
            value = values[keys[index]]
        elif isinstance(values, list):
            if index < 0 or index >= len(values):
                raise IndexError(index)
            value = values[index]
        else:
            raise IndexError(index)
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(encoded) > _MAX_NODE_BYTES:
            raise ValueError("structured node exceeds the bounded response limit")
        return value

    def derivative_path(
        self, record: ResourceRecord, derivative_id: str
    ) -> tuple[Path, dict[str, Any]]:
        if not _SAFE_DERIVATIVE_ID.fullmatch(derivative_id):
            raise KeyError(derivative_id)
        manifest = self.manifest(record) or {}
        for entry in manifest.get("entries", []):
            if entry.get("id") != derivative_id or not entry.get("content_path"):
                continue
            path = self._root(record) / "derivatives" / str(entry["content_path"])
            if path.is_file():
                return path, entry
        raise KeyError(derivative_id)

    def _load_structure(self, record: ResourceRecord) -> dict[str, Any]:
        path = self._root(record) / "structure.json"
        with self._lock:
            if not path.exists():
                raise FileNotFoundError(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("persisted document structure is invalid")
            return value


@runtime_checkable
class ResourceConverter(Protocol):
    """Developer-extensible converter contract selected by trusted MIME evidence."""

    id: str
    priority: int

    @property
    def configured(self) -> bool:
        """Return whether this converter can currently accept work."""

    @property
    def endpoint(self) -> str:
        """Return the configured service endpoint, or an empty local marker."""

    def supports(self, record: ResourceRecord) -> bool:
        """Return whether this converter accepts the detected resource type."""

    async def submit(self, record: ResourceRecord, content_path: Path) -> dict[str, Any]:
        """Submit a resource and return a converter-owned job response."""

    async def status(self, job_id: str) -> dict[str, Any]:
        """Return the current state of a converter-owned job."""


@dataclass(frozen=True)
class ConverterSubmission:
    """The converter selected by the factory and its submission response."""

    converter: ResourceConverter
    payload: dict[str, Any]


class ResourceConverterUnavailable(RuntimeError):
    """Raised when every matching converter is unavailable or rejects submission."""

    def __init__(self, media_type: str, failures: Sequence[tuple[str, BaseException]]) -> None:
        self.media_type = media_type
        self.failures = tuple(failures)
        detail = ", ".join(
            f"{converter_id}: {type(error).__name__}" for converter_id, error in failures
        )
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"no converter accepted {media_type or 'unknown content'}{suffix}")


class ResourceConverterFactory:
    """Priority-ordered converter registry with deterministic fallback."""

    def __init__(self, converters: Sequence[ResourceConverter] = ()) -> None:
        self._converters: list[ResourceConverter] = []
        for converter in converters:
            self.register(converter)

    def register(self, converter: ResourceConverter) -> None:
        """Register or replace a converter implementation by stable id."""

        converter_id = converter.id.strip()
        if not converter_id:
            raise ValueError("resource converters require a stable id")
        self._converters = [row for row in self._converters if row.id != converter_id]
        self._converters.append(converter)
        self._converters.sort(key=lambda row: (row.priority, row.id))

    def candidates(self, record: ResourceRecord) -> tuple[ResourceConverter, ...]:
        """Return configured matching converters in authoritative priority order."""

        return tuple(
            converter
            for converter in self._converters
            if converter.configured and converter.supports(record)
        )

    def get_converter(self, record: ResourceRecord) -> ResourceConverter | None:
        """Return the first configured converter for ``record`` without doing I/O."""

        return next(iter(self.candidates(record)), None)

    def get(self, converter_id: str) -> ResourceConverter | None:
        """Return a registered converter by stable id."""

        return next((row for row in self._converters if row.id == converter_id), None)

    async def submit(
        self,
        record: ResourceRecord,
        content_path: Path,
        *,
        reprocess: bool = False,
    ) -> ConverterSubmission:
        """Try matching converters in priority order until one accepts the resource.

        Reprocessing is an optional converter extension. Existing third-party
        converters keep working through ``submit`` when they do not expose it.
        """

        failures: list[tuple[str, BaseException]] = []
        for converter in self.candidates(record):
            try:
                submit = getattr(converter, "reprocess", None) if reprocess else None
                payload = await (
                    submit(record, content_path)
                    if callable(submit)
                    else converter.submit(record, content_path)
                )
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                failures.append((converter.id, exc))
                continue
            return ConverterSubmission(converter=converter, payload=payload)
        raise ResourceConverterUnavailable(record.detected_mime, failures)

    async def status(self, state: ResourceProcessingRecord) -> dict[str, Any]:
        """Poll the converter that owns a durable processing record."""

        converter = self.get(state.processor)
        if converter is None or not converter.configured:
            raise ResourceConverterUnavailable(state.processor, ())
        return await converter.status(state.job_id)

    async def cancel(self, state: ResourceProcessingRecord) -> dict[str, Any]:
        """Request cancellation from the converter that owns ``state``.

        Cancellation is an optional extension to the converter protocol so
        existing third-party converters remain compatible. The caller still
        persists CLIO's local cancelled state when a converter cannot cancel
        remote work; that prevents a late result from re-entering the session.
        """

        converter = self.get(state.processor)
        if converter is None or not converter.configured:
            raise ResourceConverterUnavailable(state.processor, ())
        cancel = getattr(converter, "cancel", None)
        if not callable(cancel):
            return {"status": "cancelled", "remote_cancelled": False}
        payload = await cancel(state.job_id)
        if not isinstance(payload, dict):
            raise ValueError("resource converter returned an invalid cancellation response")
        return {**payload, "remote_cancelled": True}

    def capabilities(self) -> list[dict[str, Any]]:
        """Return bounded registry evidence for capability negotiation."""

        return [
            {
                "id": converter.id,
                "priority": converter.priority,
                "configured": converter.configured,
                "endpoint": converter.endpoint,
            }
            for converter in self._converters
        ]


def _document_service_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt the document service's job body to the converter contract.

    Format-only, no semantic change: the service reports a failed conversion
    under ``error`` (``{"code", "stage", "message", "retryable", ...}``) while
    the converter contract — and every consumer of ``ResourceProcessingRecord``
    — reads ``failure``. Renaming it here, at the one adapter that knows this
    service, is what keeps the failure REASON reaching the trace instead of
    being replaced by a bare ``{"code": "failed"}`` placeholder.

    Nothing else is rewritten. A completed result that does not carry a
    derivative manifest is NOT patched up into one: it fails validation in
    :meth:`ResourceProcessingStore.save_result` with a typed
    ``processor_result_invalid``, which is the honest report.
    """

    error = payload.get("error")
    if isinstance(error, dict) and "failure" not in payload:
        return {**payload, "failure": error}
    return payload


class DocumentProcessorClient:
    """CLIO web-search Docling converter registered in the converter factory."""

    id = "clio-web-search-docling"
    priority = 100

    _SUPPORTED_MIME_TYPES = {
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "text/html",
    }

    def __init__(self, base_url: str, *, max_resource_bytes: int = 0) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.max_resource_bytes = max_resource_bytes
        self.submit_timeout = httpx.Timeout(
            connect=conf.resolve(
                "resources.processor_connect_timeout_s",
                env="CLIO_RESOURCE_PROCESSOR_CONNECT_TIMEOUT_S",
                default=5.0,
                cast=conf.as_float,
            ),
            read=conf.resolve(
                "resources.processor_read_timeout_s",
                env="CLIO_RESOURCE_PROCESSOR_READ_TIMEOUT_S",
                default=60.0,
                cast=conf.as_float,
            ),
            write=self._write_timeout(),
            pool=conf.resolve(
                "resources.processor_pool_timeout_s",
                env="CLIO_RESOURCE_PROCESSOR_POOL_TIMEOUT_S",
                default=5.0,
                cast=conf.as_float,
            ),
        )
        self.status_timeout = conf.resolve(
            "resources.processor_status_timeout_s",
            env="CLIO_RESOURCE_PROCESSOR_STATUS_TIMEOUT_S",
            default=30.0,
            cast=conf.as_float,
        )
        self.cancel_timeout = conf.resolve(
            "resources.processor_cancel_timeout_s",
            env="CLIO_RESOURCE_PROCESSOR_CANCEL_TIMEOUT_S",
            default=30.0,
            cast=conf.as_float,
        )

    def _write_timeout(self) -> float:
        """Resolve the submit write timeout, DERIVING it from the upload ceiling.

        A pinned write timeout that is shorter than the time it takes to stream
        ``resources.max_bytes`` at a slow link speed truncates exactly the large
        scientific uploads the ceiling exists to allow. Left at ``0`` (the
        default) the timeout is derived from that ceiling at
        :data:`_PROCESSOR_MIN_UPLOAD_BYTES_PER_S`; any positive value pins it.
        """

        configured = conf.resolve(
            "resources.processor_write_timeout_s",
            env="CLIO_RESOURCE_PROCESSOR_WRITE_TIMEOUT_S",
            default=0.0,
            cast=conf.as_float,
        )
        if configured > 0:
            return configured
        derived = float(self.max_resource_bytes) / _PROCESSOR_MIN_UPLOAD_BYTES_PER_S
        return max(_PROCESSOR_WRITE_TIMEOUT_FLOOR_S, derived)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def endpoint(self) -> str:
        """Return the configured CLIO web-search endpoint."""

        return self.base_url

    def supports(self, record: ResourceRecord) -> bool:
        """Select from server-detected MIME, never the client claim or suffix alone."""

        return record.detected_mime in self._SUPPORTED_MIME_TYPES

    async def _submit(
        self,
        record: ResourceRecord,
        content_path: Path,
        *,
        force: bool,
    ) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("document processor is not configured")
        with content_path.open("rb") as stream:
            async with httpx.AsyncClient(timeout=self.submit_timeout) as client:
                response = await client.post(
                    f"{self.base_url}/v1/documents",
                    files={"file": (record.name, stream, record.detected_mime)},
                    data={"force": "true"} if force else None,
                )
                response.raise_for_status()
                value = response.json()
        if not isinstance(value, dict) or not value.get("id"):
            raise ValueError("document processor returned an invalid submission")
        return _document_service_payload(value)

    async def submit(self, record: ResourceRecord, content_path: Path) -> dict[str, Any]:
        """Submit normally, allowing the document service to reuse a cached result."""

        return await self._submit(record, content_path, force=False)

    async def reprocess(self, record: ResourceRecord, content_path: Path) -> dict[str, Any]:
        """Request a fresh conversion for an explicitly reprocessed resource."""

        return await self._submit(record, content_path, force=True)

    async def status(self, job_id: str) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("document processor is not configured")
        async with httpx.AsyncClient(timeout=self.status_timeout) as client:
            response = await client.get(f"{self.base_url}/v1/documents/{job_id}")
            response.raise_for_status()
            value = response.json()
        if not isinstance(value, dict):
            raise ValueError("document processor returned an invalid status")
        return _document_service_payload(value)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued or active Docling conversion."""

        if not self.configured:
            raise RuntimeError("document processor is not configured")
        async with httpx.AsyncClient(timeout=self.cancel_timeout) as client:
            response = await client.post(f"{self.base_url}/v1/documents/{job_id}/cancel")
            response.raise_for_status()
            value = response.json()
        if not isinstance(value, dict):
            raise ValueError("document processor returned an invalid cancellation response")
        return value


__all__ = [
    "ConverterSubmission",
    "DocumentProcessorClient",
    "ResourceConverter",
    "ResourceConverterFactory",
    "ResourceConverterUnavailable",
    "ResourceCustodyGone",
    "ResourceProcessingRecord",
    "ResourceProcessingStore",
]
