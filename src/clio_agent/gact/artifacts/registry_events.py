"""Fold-event normalization for the artifact registry (owner module, no accretion).

Split out of :mod:`clio_agent.gact.artifacts.registry`: one source-agnostic shape
(:class:`_ArtifactEvent`) read from an ARC ``_events`` segment, a JSONL trace line
or a freshly minted payload, and its reconstruction into an
:class:`~clio_agent.gact.artifacts.records.ArtifactVersion`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactVersion,
    Custody,
    EvidenceClass,
    IdentityEvidence,
    Mechanism,
    new_artifact_id,
)


@dataclass(frozen=True)
class _ArtifactEvent:
    """A normalized artifact event, extracted from a semantic event's payload.

    Source-agnostic: the same shape is read from an ARC ``_events`` segment, a
    JSONL trace line, or a freshly minted event — so the fold never branches on
    provenance.
    """

    event_id: str
    workspace_id: str
    name: str
    version: int
    artifact_id: str
    sha256: Optional[str]
    size_bytes: Optional[int]
    kind: str
    custody: str
    mechanism: str
    evidence_class: str
    mtime: Optional[float]
    authority: str
    producer: dict[str, Any]
    path: str
    created_at: str
    annotation: str
    #: S4 revision-edge + custody markers (on ``version.added``; defaulted on v1).
    prior_version: Optional[int]
    prior_sha256: Optional[str]
    kind_warning: str
    custody_gap: Optional[dict[str, Any]]
    #: S6 over-threshold non-ingestion marker (bytes; ``None`` when ingested/small).
    not_ingested_size: Optional[int]


def _artifact_event_from_payload(payload: dict[str, Any]) -> Optional[_ArtifactEvent]:
    """Parse an ``artifact.created`` payload into a normalized event, or ``None``.

    Returns ``None`` when the payload lacks the load-bearing identity fields
    (workspace/name/version) — a malformed record is dropped from the fold with a
    typed reason by the caller, never crashes the boot.
    """
    ws = str(payload.get("workspace_id") or "")
    name = str(payload.get("name") or "")
    if not name:
        return None
    try:
        version = int(payload.get("version") or 0)
    except (TypeError, ValueError):
        return None
    if version < 1:
        return None
    raw_evidence = payload.get("evidence")
    evidence: dict[str, Any] = raw_evidence if isinstance(raw_evidence, dict) else {}
    return _ArtifactEvent(
        event_id=str(payload.get("event_id") or ""),
        workspace_id=ws,
        name=name,
        version=version,
        artifact_id=str(payload.get("artifact_id") or ""),
        sha256=(str(payload["sha256"]) if payload.get("sha256") else None),
        size_bytes=(int(payload["size_bytes"]) if payload.get("size_bytes") is not None else None),
        kind=str(payload.get("kind") or ArtifactKind.OTHER.value),
        custody=str(payload.get("custody") or Custody.WORKSPACE_REFERENCED.value),
        mechanism=str(payload.get("mechanism") or Mechanism.TOOL_SCHEMA.value),
        evidence_class=str(
            (evidence.get("evidence_class") if evidence else None)
            or payload.get("evidence_class")
            or EvidenceClass.HASHED_AT_USE.value
        ),
        mtime=(
            float(evidence["mtime"])
            if evidence.get("mtime") is not None
            else (float(payload["mtime"]) if payload.get("mtime") is not None else None)
        ),
        authority=str((evidence.get("authority") if evidence else "") or ""),
        producer=dict(payload.get("producer") or {}),
        path=str(payload.get("path") or ""),
        created_at=str(payload.get("created_at") or ""),
        annotation=str(payload.get("annotation") or ""),
        prior_version=(
            int(payload["prior_version"]) if payload.get("prior_version") is not None else None
        ),
        prior_sha256=(str(payload["prior_sha256"]) if payload.get("prior_sha256") else None),
        kind_warning=str(payload.get("kind_warning") or ""),
        custody_gap=(
            dict(payload["custody_gap"]) if isinstance(payload.get("custody_gap"), dict) else None
        ),
        not_ingested_size=(
            int(payload["not_ingested_size"])
            if payload.get("not_ingested_size") is not None
            else None
        ),
    )


def _version_from_event(event: _ArtifactEvent) -> ArtifactVersion:
    """Reconstruct an :class:`ArtifactVersion` from a normalized fold event."""
    try:
        evidence_class = EvidenceClass(event.evidence_class)
    except ValueError:
        evidence_class = EvidenceClass.HASHED_AT_USE
    evidence = IdentityEvidence(
        evidence_class=evidence_class,
        sha256=event.sha256,
        size_bytes=event.size_bytes,
        mtime=event.mtime,
        authority=event.authority,
    )
    return ArtifactVersion(
        artifact_id=event.artifact_id or new_artifact_id(),
        version=event.version,
        kind=_safe_kind(event.kind),
        custody=_safe_custody(event.custody),
        mechanism=_safe_mechanism(event.mechanism),
        evidence=evidence,
        producer=event.producer,
        path=event.path,
        created_at=event.created_at,
        annotation=event.annotation,
        prior_version=event.prior_version,
        prior_sha256=event.prior_sha256,
        kind_warning=event.kind_warning,
        custody_gap=event.custody_gap,
        not_ingested_size=event.not_ingested_size,
    )


def _safe_kind(value: str) -> ArtifactKind:
    try:
        return ArtifactKind(value)
    except ValueError:
        return ArtifactKind.OTHER


def _safe_custody(value: str) -> Custody:
    try:
        return Custody(value)
    except ValueError:
        return Custody.WORKSPACE_REFERENCED


def _safe_mechanism(value: str) -> Mechanism:
    try:
        return Mechanism(value)
    except ValueError:
        return Mechanism.TOOL_SCHEMA
