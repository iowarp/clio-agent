"""Persistent resource delivery decisions bound to message and model identity."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from clio_agent import conf
from clio_agent.gact.resource_custody import ResourceRecord, quarantine_corrupt_index
from clio_agent.gact.types import ModelRef

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _delivery_ledger_max_records() -> int:
    """Rows of delivery provenance retained before the oldest are compacted away."""

    return max(
        1,
        conf.resolve(
            "resources.delivery_ledger_max_records",
            env="CLIO_RESOURCE_DELIVERY_LEDGER_MAX_RECORDS",
            default=2000,
            cast=conf.as_int,
        ),
    )


# The representations the delivery PATH actually implements. ``native`` means
# the resource's own bytes reach the model; image and PDF lanes are implemented.
# ``retrieval`` and ``sandbox`` were removed: the
# first was never produced by any branch, the second was produced but nothing
# consumed it, so both were ledger entries describing work that never happened.
DeliveryRepresentation = Literal[
    "native",
    "bounded_tools",
    "structured_document",
    "metadata_only",
]

# Typed delivery reasons, in the stream_fallback reason-catalog style: the code
# is the queryable fact, the sentence is what a human reads.
DELIVERY_REASONS: dict[str, str] = {
    "native_image_input": "live model handshake reports image input",
    "native_pdf_input": "live model handshake reports PDF input",
    "native_lane_unimplemented": (
        "the live model handshake reports {modality} input, but CLIO implements native "
        "delivery for this modality; this resource is planned as {representation} instead"
    ),
    "bounded_text_tools": "text is exposed through bounded resource tools",
    "structured_document": (
        "native capability is unverified or unimplemented; preserve the structured "
        "document conversion for bounded tools"
    ),
    "opaque_binary": (
        "opaque scientific or archive data has no safe verified content representation"
    ),
    "no_representation": "no safe, verified content representation exists",
}

# Prefix media the native lane can actually carry today.
NATIVE_MEDIA_PREFIXES = ("image/",)


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
    reason_code: str = ""
    reason: str
    delivered_at: str = Field(default_factory=_now_iso)


class ResourceDeliveryStore:
    """Small append-only JSON ledger for resource delivery provenance."""

    def __init__(self, path: Path, *, max_records: int = 0) -> None:
        self.path = path
        self.max_records = max_records or _delivery_ledger_max_records()
        self._lock = threading.RLock()
        self._rows: list[ResourceDeliveryRecord] = []
        self.load_degradation: dict[str, str] | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = [ResourceDeliveryRecord(**row) for row in payload.get("records", [])]
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.load_degradation = quarantine_corrupt_index(
                self.path, exc, kind="resource_delivery_ledger"
            )
            self._rows = []
            return
        self._rows = self._compact_locked(rows)

    def _compact_locked(self, rows: list[ResourceDeliveryRecord]) -> list[ResourceDeliveryRecord]:
        """Drop the oldest provenance beyond the retention cap, reporting the loss.

        The ledger is rewritten in full on every append, so an uncapped file is
        both unbounded disk AND an O(n) write per delivered attachment.
        Retention keeps the most recent ``resources.delivery_ledger_max_records``
        rows; the drop is logged with a typed reason rather than happening
        quietly.
        """

        overflow = len(rows) - self.max_records
        if overflow <= 0:
            return rows
        logger.info(
            "compacted resource delivery ledger reason=delivery_ledger_retention "
            "dropped=%d retained=%d path=%s",
            overflow,
            self.max_records,
            self.path,
        )
        return rows[overflow:]

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
            self._rows = self._compact_locked([*self._rows, record])
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


#: Evidence-source labels a delivery plan may treat as real capability evidence.
#: ``live_handshake`` is a probe this process ran; ``discovery_overlay`` is a
#: persisted earlier discovery run (the Codex SDK catalog read / the claude_code
#: alias probe) served through the passive handshake. Both were produced by
#: asking the provider. Anything else -- notably ``unavailable`` -- is not
#: evidence and can never justify handing the model an attachment's bytes.
EVIDENCED_MODALITY_SOURCES: frozenset[str] = frozenset({"live_handshake", "discovery_overlay"})

#: HandshakeReport.models_source -> the delivery-plan evidence label it earns.
_EVIDENCE_LABEL_BY_SOURCE: dict[str, str] = {
    "live": "live_handshake",
    "overlay": "discovery_overlay",
}


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
    # ``evidenced`` covers both a live probe and a persisted discovery run; the
    # older ``live`` key is honoured for a catalog payload written before the
    # distinction existed, so an in-flight app's cached dict is not misread.
    evidenced = evidence.get("evidenced")
    if evidenced is None:
        evidenced = evidence.get("live") is True
    generated_at = str(evidence.get("generated_at") or "")
    if evidenced is not True:
        return {"text"}, "unavailable", generated_at
    label = _EVIDENCE_LABEL_BY_SOURCE.get(str(evidence.get("source") or ""), "live_handshake")
    return _modalities(profile.get("modalities")), label, generated_at


def _live_modalities(app: Any, model: ModelRef) -> tuple[set[str], str, str]:
    report = getattr(app.state, "lm_handshake_report", None)
    if (
        report is not None
        and report.ok
        and report.models_source in _EVIDENCE_LABEL_BY_SOURCE
        and model.provider_id in {report.provider_id, report.provider_kind, ""}
    ):
        profile = report.model(model.model_id)
        if profile is not None:
            # The evidence's OWN timestamp, not the wall clock of the handshake
            # run that read it -- a cached catalog must not date itself to now.
            return (
                _modalities(profile.capabilities),
                _EVIDENCE_LABEL_BY_SOURCE[report.models_source],
                profile.evidence_generated_at
                or getattr(report, "evidence_generated_at", "")
                or report.generated_at,
            )
    return _catalog_modalities(app, model)


def live_model_modalities(app: Any, model: ModelRef) -> tuple[set[str], str, str]:
    """Return live-evidenced modalities for one exact provider/model selection."""

    return _live_modalities(app, model)


_TEXTUAL_TYPES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
    }
)

_STRUCTURED_DOCUMENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)

_OPAQUE_TYPES = frozenset(
    {
        "application/x-hdf5",
        "application/x-netcdf",
        "application/zip",
        "application/gzip",
        "application/x-tar",
    }
)

# Media whose modality the handshake reports but whose native delivery lane is
# NOT implemented. Kept as explicit maps so the day a lane lands, the seam is
# one entry, and until then the trace names exactly what was skipped.
_UNIMPLEMENTED_NATIVE_EXACT: dict[str, str] = {}
_UNIMPLEMENTED_NATIVE_PREFIXES: dict[str, str] = {"audio/": "audio", "video/": "video"}


def _unimplemented_native_modality(media_type: str, modalities: set[str]) -> str:
    """Return the modality a capable model offers but CLIO cannot deliver natively."""

    modality = _UNIMPLEMENTED_NATIVE_EXACT.get(media_type, "")
    if not modality:
        modality = next(
            (
                value
                for prefix, value in _UNIMPLEMENTED_NATIVE_PREFIXES.items()
                if media_type.startswith(prefix)
            ),
            "",
        )
    return modality if modality and modality in modalities else ""


def _representation_for(
    media_type: str, modalities: set[str]
) -> tuple[DeliveryRepresentation, str, str]:
    """Return the representation, its typed reason code, and the reason sentence.

    A plan may only say ``native`` for media the delivery path actually carries
    (images and PDFs). When a model reports a modality CLIO has no native lane for, the
    plan falls through to the next HONEST representation for that media — the
    structured conversion for documents, metadata for opaque media — and records
    ``native_lane_unimplemented`` naming the skipped modality, so the ledger,
    ``resource.delivery_resolved`` and the v3 provenance stop claiming a
    delivery that never happens.
    """

    if media_type.startswith(NATIVE_MEDIA_PREFIXES) and "image" in modalities:
        return "native", "native_image_input", DELIVERY_REASONS["native_image_input"]
    if media_type == "application/pdf" and "pdf" in modalities:
        return "native", "native_pdf_input", DELIVERY_REASONS["native_pdf_input"]
    skipped = _unimplemented_native_modality(media_type, modalities)
    if media_type.startswith("text/") or media_type in _TEXTUAL_TYPES:
        representation: DeliveryRepresentation = "bounded_tools"
        code = "bounded_text_tools"
    elif media_type in _STRUCTURED_DOCUMENT_TYPES:
        representation, code = "structured_document", "structured_document"
    elif media_type in _OPAQUE_TYPES:
        representation, code = "metadata_only", "opaque_binary"
    else:
        representation, code = "metadata_only", "no_representation"
    if skipped:
        return (
            representation,
            "native_lane_unimplemented",
            DELIVERY_REASONS["native_lane_unimplemented"].format(
                modality=skipped, representation=representation
            ),
        )
    return representation, code, DELIVERY_REASONS[code]


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
    representation, reason_code, reason = _representation_for(media_type, modalities)
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
        reason_code=reason_code,
        reason=reason,
    )


__all__ = [
    "DELIVERY_REASONS",
    "EVIDENCED_MODALITY_SOURCES",
    "DeliveryRepresentation",
    "ResourceDeliveryRecord",
    "ResourceDeliveryStore",
    "live_model_modalities",
    "plan_resource_delivery",
]
