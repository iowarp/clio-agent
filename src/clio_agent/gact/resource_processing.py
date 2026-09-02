"""Extensible structured conversion for workspace-owned resources.

The custody layer deliberately knows nothing about Docling.  Converters are
registered here with an explicit priority and selected from the resource's
server-detected MIME type.  The CLIO web-search Docling service is the first
built-in implementation, not a hard-coded processing path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field

from clio_agent.gact.resource_custody import ResourceRecord, ResourceStore

_SAFE_DERIVATIVE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_NODE_BYTES = 2 * 1024 * 1024
_ALLOWED_NODE_COLLECTIONS = {"pages", "tables", "pictures", "texts"}
RESOURCE_CONVERTER_ENTRY_POINT_GROUP = "clio_agent.resource_converters"
logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_docling_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt clio-web-search's completed result to CLIO's derivative contract.

    The document service owns Docling execution and returns its canonical document plus
    Markdown directly.  Resource custody persists those values through a generic named-
    derivative manifest, so the built-in converter performs that service-specific adapter
    step without imposing the Docling response shape on third-party converters.
    """

    result = payload.get("result")
    if str(payload.get("status") or "") != "complete" or not isinstance(result, dict):
        return payload
    manifest = result.get("derivatives")
    if isinstance(manifest, dict) and isinstance(manifest.get("entries"), list):
        return payload
    document = result.get("document")
    if not isinstance(document, dict) or not isinstance(document.get("structure"), dict):
        return payload
    entries: list[dict[str, Any]] = []
    markdown = result.get("markdown")
    if isinstance(markdown, str):
        entries.append(
            {
                "id": "markdown",
                "name": "document.md",
                "kind": "markdown",
                "media_type": "text/markdown",
                "content": markdown,
            }
        )
    normalized = {
        **result,
        "derivatives": {
            "schema": "clio.resource-derivatives.v1",
            "entries": entries,
        },
    }
    return {**payload, "result": normalized}


class ResourceProcessingRecord(BaseModel):
    """Durable state for one resource revision's processing job."""

    workspace_id: str
    resource_id: str
    resource_revision: int
    source_sha256: str
    processor: str = ""
    processor_url: str = ""
    job_id: str = ""
    query_tool: str = "workspace_resource_inspect"
    state: Literal["not_started", "submitted", "processing", "complete", "failed", "cancelled"] = (
        "not_started"
    )
    progress: int = 0
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

    def save_state(self, record: ResourceRecord, state: ResourceProcessingRecord) -> None:
        with self._lock:
            root = self._root(record)
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
        root = self._root(record)
        derivatives_root = root / "derivatives"
        with self._lock:
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
                    destination = derivatives_root / derivative_id
                    destination.write_text(content, encoding="utf-8")
                    entry["content_path"] = destination.name
                    entry["size"] = destination.stat().st_size
                persisted_entries.append(entry)
            saved_manifest = {
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

    def discover_entry_points(
        self, group: str = RESOURCE_CONVERTER_ENTRY_POINT_GROUP
    ) -> tuple[str, ...]:
        """Load installed no-argument converter factories from ``group``.

        A third-party package can publish an entry point whose target is either
        a converter instance or a no-argument callable returning one. Broken
        extensions are isolated and logged so they cannot prevent CLIO from
        starting or suppress the built-in fallback chain.
        """

        registered: list[str] = []
        for entry_point in metadata.entry_points(group=group):
            try:
                loaded = entry_point.load()
                converter = (
                    loaded
                    if not isinstance(loaded, type) and isinstance(loaded, ResourceConverter)
                    else loaded()
                )
                if not isinstance(converter, ResourceConverter):
                    raise TypeError("entry point did not return a ResourceConverter")
                self.register(converter)
            except Exception as exc:  # noqa: BLE001 - installed extension isolation boundary
                logger.warning(
                    "Ignoring resource converter entry point %s: %s",
                    entry_point.name,
                    exc,
                )
                continue
            registered.append(converter.id)
        return tuple(registered)

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

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.strip().rstrip("/")

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
        timeout = httpx.Timeout(connect=5, read=60, write=60, pool=5)
        with content_path.open("rb") as stream:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.base_url}/v1/documents",
                    files={"file": (record.name, stream, record.detected_mime)},
                    data={"force": "true"} if force else None,
                )
                response.raise_for_status()
                value = response.json()
        if not isinstance(value, dict) or not value.get("id"):
            raise ValueError("document processor returned an invalid submission")
        return _normalize_docling_payload(value)

    async def submit(self, record: ResourceRecord, content_path: Path) -> dict[str, Any]:
        """Submit normally, allowing the document service to reuse a cached result."""

        return await self._submit(record, content_path, force=False)

    async def reprocess(self, record: ResourceRecord, content_path: Path) -> dict[str, Any]:
        """Request a fresh conversion for an explicitly reprocessed resource."""

        return await self._submit(record, content_path, force=True)

    async def status(self, job_id: str) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("document processor is not configured")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base_url}/v1/documents/{job_id}")
            response.raise_for_status()
            value = response.json()
        if not isinstance(value, dict):
            raise ValueError("document processor returned an invalid status")
        return _normalize_docling_payload(value)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued or active Docling conversion."""

        if not self.configured:
            raise RuntimeError("document processor is not configured")
        async with httpx.AsyncClient(timeout=30) as client:
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
    "RESOURCE_CONVERTER_ENTRY_POINT_GROUP",
    "ResourceProcessingRecord",
    "ResourceProcessingStore",
]
