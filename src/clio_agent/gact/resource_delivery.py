"""Persistent resource delivery decisions bound to message and model identity."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from clio_agent.gact.resource_custody import ResourceRecord
from clio_agent.gact.types import ModelRef


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DeliveryRepresentation = Literal[
    "native",
    "bounded_tools",
    "structured_document",
    "sandbox",
    "retrieval",
    "metadata_only",
]


class ResourceDeliveryRecord(BaseModel):
    """One immutable route decision for one resource revision and message."""

    id: str = Field(default_factory=lambda: "rdl_" + uuid.uuid4().hex)
    workspace_id: str
    resource_id: str
    resource_revision: int
    resource_sha256: str
    message_id: str
    provider_id: str
    model_id: str
    representation: DeliveryRepresentation
    evidence_source: str
    evidence_generated_at: str = ""
    reason: str
    delivered_at: str = Field(default_factory=_now_iso)


class ResourceDeliveryStore:
    """Small append-only JSON ledger for resource delivery provenance."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._rows: list[ResourceDeliveryRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._rows = [ResourceDeliveryRecord(**row) for row in payload.get("records", [])]
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"resource delivery ledger is unreadable: {exc}") from exc

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps({"records": [row.model_dump() for row in self._rows]}, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def append(self, record: ResourceDeliveryRecord) -> ResourceDeliveryRecord:
        with self._lock:
            prior = next(
                (
                    row
                    for row in self._rows
                    if row.message_id == record.message_id
                    and row.resource_id == record.resource_id
                    and row.resource_revision == record.resource_revision
                ),
                None,
            )
            if prior is not None:
                return prior.model_copy(deep=True)
            self._rows.append(record)
            self._flush_locked()
            return record.model_copy(deep=True)

    def list(self, workspace_id: str) -> list[ResourceDeliveryRecord]:
        with self._lock:
            return [
                row.model_copy(deep=True) for row in self._rows if row.workspace_id == workspace_id
            ]

    def delete_resource(self, workspace_id: str, resource_id: str) -> int:
        with self._lock:
            before = len(self._rows)
            self._rows = [
                row
                for row in self._rows
                if not (row.workspace_id == workspace_id and row.resource_id == resource_id)
            ]
            removed = before - len(self._rows)
            if removed:
                self._flush_locked()
            return removed

    def delete_workspace(self, workspace_id: str) -> int:
        """Remove every delivery record owned by a deleted workspace."""

        with self._lock:
            before = len(self._rows)
            self._rows = [row for row in self._rows if row.workspace_id != workspace_id]
            removed = before - len(self._rows)
            if removed:
                self._flush_locked()
            return removed


def _modalities(values: Any) -> set[str]:
    if not isinstance(values, (list, tuple, set)):
        return {"text"}
    normalized = {str(value).lower().replace("-", "_") for value in values}
    modalities = {"text"}
    if normalized & {"vision", "image", "images", "image_input"}:
        modalities.add("image")
    if normalized & {"pdf", "document", "documents", "pdf_input"}:
        modalities.add("pdf")
    if normalized & {"audio", "audio_input"}:
        modalities.add("audio")
    if normalized & {"video", "video_input"}:
        modalities.add("video")
    return modalities


def _catalog_modalities(app: Any, model: ModelRef) -> tuple[set[str], str, str]:
    catalog = getattr(app.state, "provider_catalog", None)
    if not isinstance(catalog, dict):
        return {"text"}, "unavailable", ""
    providers = catalog.get("providers")
    if not isinstance(providers, list):
        return {"text"}, "unavailable", ""
    provider = next(
        (
            row
            for row in providers
            if isinstance(row, dict)
            and row.get("id") == model.provider_id
            and row.get("health") == "ready"
        ),
        None,
    )
    if provider is None or not isinstance(provider.get("models"), list):
        return {"text"}, "unavailable", ""
    profile = next(
        (
            row
            for row in provider["models"]
            if isinstance(row, dict)
            and row.get("model_id") == model.model_id
            and row.get("availability") == "available"
        ),
        None,
    )
    if profile is None or not isinstance(profile.get("evidence"), dict):
        return {"text"}, "unavailable", ""
    evidence = profile["evidence"]
    if evidence.get("live") is not True:
        return {"text"}, "unavailable", str(evidence.get("generated_at") or "")
    return (
        _modalities(profile.get("modalities")),
        "live_handshake",
        str(evidence.get("generated_at") or ""),
    )


def _live_modalities(app: Any, model: ModelRef) -> tuple[set[str], str, str]:
    report = getattr(app.state, "lm_handshake_report", None)
    if (
        report is not None
        and report.ok
        and report.models_source == "live"
        and model.provider_id in {report.provider_id, report.provider_kind, ""}
    ):
        profile = report.model(model.model_id)
        if profile is not None:
            return _modalities(profile.capabilities), "live_handshake", report.generated_at
    return _catalog_modalities(app, model)


def live_model_modalities(app: Any, model: ModelRef) -> tuple[set[str], str, str]:
    """Return live-evidenced modalities for one exact provider/model selection."""

    return _live_modalities(app, model)


def plan_resource_delivery(
    app: Any,
    *,
    resource: ResourceRecord,
    message_id: str,
    model: ModelRef,
) -> ResourceDeliveryRecord:
    """Choose an honest representation without changing the selected provider."""

    media_type = resource.detected_mime
    modalities, evidence, generated_at = _live_modalities(app, model)
    representation: DeliveryRepresentation
    reason: str
    if media_type.startswith("image/") and "image" in modalities:
        representation, reason = "native", "live model handshake reports image input"
    elif media_type == "application/pdf" and "pdf" in modalities:
        representation, reason = "native", "live model handshake reports PDF input"
    elif media_type.startswith("audio/") and "audio" in modalities:
        representation, reason = "native", "live model handshake reports audio input"
    elif media_type.startswith("video/") and "video" in modalities:
        representation, reason = "native", "live model handshake reports video input"
    elif media_type.startswith("text/") or media_type in {
        "application/json",
        "application/javascript",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    }:
        representation, reason = "bounded_tools", "text is exposed through bounded resource tools"
    elif media_type in {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        representation, reason = (
            "structured_document",
            "native capability is unverified; preserve Docling structure for bounded tools",
        )
    elif media_type in {"application/x-hdf5", "application/x-netcdf", "application/zip"}:
        representation, reason = "sandbox", "opaque scientific or archive data requires a sandbox"
    else:
        representation, reason = "metadata_only", "no safe, verified content representation exists"
    return ResourceDeliveryRecord(
        workspace_id=resource.workspace_id,
        resource_id=resource.id,
        resource_revision=resource.revision,
        resource_sha256=resource.sha256,
        message_id=message_id,
        provider_id=model.provider_id,
        model_id=model.model_id,
        representation=representation,
        evidence_source=evidence,
        evidence_generated_at=generated_at,
        reason=reason,
    )


__all__ = [
    "ResourceDeliveryRecord",
    "ResourceDeliveryStore",
    "live_model_modalities",
    "plan_resource_delivery",
]
