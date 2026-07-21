"""In-memory artifact registry — projection, fold semantics, minting, boot rebuild.

Storage is a PROJECTION over the existing semantic-event log (RULE 4 / #737 —
owner decision #966.8), not a new store: the authoritative capture is
``artifact.*`` semantic events emitted via ``_emit_semantic_event`` (ARC
``_events`` + durable trace); the in-memory :class:`ArtifactRegistry` on
``app.state.artifact_registry`` rebuilds from the fold set on first access; a
SMALL bounded SessionStore ``metadata`` patch is a fast badge index only, NEVER
the rebuild source.

Fold idempotency (owner decision #966): dedupe by ``event_id`` first, then by
``(workspace_id, name, version)``; a same-sha replay is a no-op; a conflicting
sha for an existing ``(ws, name, version)`` keeps the FIRST and records a typed
``fold_conflict``. Events are trace-only this slice — minting does NOT add
``artifact.created`` to ``SSE_UI_EVENT_TYPES`` (that is S2).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Custody,
    EvidenceClass,
    IdentityEvidence,
    Mechanism,
    new_artifact_id,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

ARTIFACT_CREATED_EVENT = "artifact.created"

# Bounded SessionStore badge index: at most this many named artifacts are listed
# per session before the index truncates (``names_truncated=True``). The index is
# badges only — the full set always rebuilds from the event log, never from here.
_SESSION_INDEX_NAME_CAP = 64

#: Default ceiling on hashing a designated output at mint. Over this, the version
#: is recorded ``stat-pinned`` (typed, permanent) rather than paying multi-GB I/O
#: on the turn thread (design resolution 5b). Config-first (#985 conventions).
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
    )


@dataclass
class FoldResult:
    """Typed outcome of folding one artifact event.

    Exactly one of the boolean flags is the operative result; ``reason`` carries a
    machine tag for the degraded paths (``duplicate_event_id`` /
    ``duplicate_version`` / ``same_sha_replay`` / ``fold_conflict`` /
    ``malformed``) so no fold degradation is silent.
    """

    applied: bool = False
    reason: str = ""
    version: Optional[ArtifactVersion] = None


class ArtifactRegistry:
    """In-memory projection of the artifact event log, rebuilt at boot.

    Thread-safe (a single lock guards the chains). The fold is idempotent and
    order-tolerant enough for replay: it dedupes by ``event_id`` then by
    ``(workspace_id, name, version)`` and keeps the first content-hash on a
    conflict. It never reads the SessionStore badge index — that index is a
    projection OF this registry, never a source FOR it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], ArtifactRecord] = {}
        self._seen_event_ids: set[str] = set()
        self.fold_conflicts: list[dict[str, Any]] = []
        #: Typed reason recorded when boot found no fold source at all.
        self.capture_released: Optional[dict[str, Any]] = None

    # ---- fold --------------------------------------------------------------

    def fold_payload(self, payload: dict[str, Any]) -> FoldResult:
        """Fold one ``artifact.created`` payload into the registry."""
        event = _artifact_event_from_payload(payload)
        if event is None:
            return FoldResult(applied=False, reason="malformed")
        return self._fold_event(event)

    def _fold_event(self, event: _ArtifactEvent) -> FoldResult:
        with self._lock:
            if event.event_id and event.event_id in self._seen_event_ids:
                return FoldResult(applied=False, reason="duplicate_event_id")
            key = (event.workspace_id, event.name)
            record = self._records.get(key)
            if record is None:
                record = ArtifactRecord(workspace_id=event.workspace_id, name=event.name)
                self._records[key] = record

            existing = next((v for v in record.versions if v.version == event.version), None)
            if existing is not None:
                if event.event_id:
                    self._seen_event_ids.add(event.event_id)
                if existing.sha256 == event.sha256:
                    # Same (ws, name, version) with the same content — replay no-op.
                    return FoldResult(
                        applied=False,
                        reason="same_sha_replay" if event.sha256 else "duplicate_version",
                        version=existing,
                    )
                # Conflicting sha for the same version — keep first, record typed.
                conflict = {
                    "workspace_id": event.workspace_id,
                    "name": event.name,
                    "version": event.version,
                    "kept_sha256": existing.sha256,
                    "rejected_sha256": event.sha256,
                    "rejected_artifact_id": event.artifact_id,
                }
                self.fold_conflicts.append(conflict)
                logger.warning(
                    "artifact fold_conflict reason=version_sha_mismatch ws=%s name=%s "
                    "version=%d kept=%s rejected=%s",
                    event.workspace_id,
                    event.name,
                    event.version,
                    existing.sha256,
                    event.sha256,
                )
                return FoldResult(applied=False, reason="fold_conflict", version=existing)

            version = _version_from_event(event)
            record.add_version(version)
            if event.event_id:
                self._seen_event_ids.add(event.event_id)
            return FoldResult(applied=True, reason="", version=version)

    # ---- queries -----------------------------------------------------------

    def get(self, workspace_id: str, name: str) -> Optional[ArtifactRecord]:
        """Return the logical record for ``(workspace_id, name)``, or ``None``."""
        with self._lock:
            return self._records.get((workspace_id, name))

    def list_for_workspace(self, workspace_id: str) -> list[ArtifactRecord]:
        """Every logical artifact in a workspace."""
        with self._lock:
            return [r for (ws, _n), r in self._records.items() if ws == workspace_id]

    def count(self) -> int:
        """Total number of logical artifacts known."""
        with self._lock:
            return len(self._records)

    def all_records(self) -> list[ArtifactRecord]:
        """A snapshot of every logical record (for introspection / tests)."""
        with self._lock:
            return list(self._records.values())


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


def get_registry(app: "FastAPI") -> ArtifactRegistry:
    """Return the app's artifact registry, rebuilding it from the log on first access.

    The projection rebuilds LAZILY (RULE 4 / #737): the first consumer — a mint
    or a query — triggers :func:`rebuild_registry_at_boot`, which folds the durable
    ``artifact.created`` events (ARC ``_events``, else the JSONL trace; neither →
    typed ``capture_released``). This keeps the boot seam out of the ``build_app``
    god file while still rebuilding once, before first use.
    """
    registry = getattr(app.state, "artifact_registry", None)
    if registry is None:
        registry = rebuild_registry_at_boot(app)
    return registry


# --------------------------------------------------------------------------- #
# SessionStore small badge index (badges only — NEVER the rebuild source)
# --------------------------------------------------------------------------- #


def build_session_index(registry: ArtifactRegistry, workspace_id: str) -> dict[str, Any]:
    """Build the bounded per-workspace badge index.

    Shape: ``{count, names: {name: {v, id, kind}}, names_truncated}`` — small,
    bounded (:data:`_SESSION_INDEX_NAME_CAP` names), for quick session badges. The
    full artifact set always rebuilds from the event log; this is never read back
    as a source (owner decision #966.8 / #966.4).
    """
    records = sorted(registry.list_for_workspace(workspace_id), key=lambda r: r.name)
    names: dict[str, Any] = {}
    truncated = False
    for record in records:
        head = record.head
        if head is None:
            continue
        if len(names) >= _SESSION_INDEX_NAME_CAP:
            truncated = True
            break
        names[record.name] = {
            "v": head.version,
            "id": head.artifact_id,
            "kind": head.kind.value,
        }
    return {"count": len(records), "names": names, "names_truncated": truncated}


def patch_session_index(
    app: "FastAPI", sid: str, registry: ArtifactRegistry, workspace_id: str
) -> None:
    """Stamp the bounded badge index onto the session's metadata (best-effort)."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return
    index = build_session_index(registry, workspace_id)
    try:
        store.update(sid, metadata_patch={"artifacts": index})
    except Exception:  # noqa: BLE001 — a badge stamp must never break a turn
        logger.warning(
            "artifact session-index stamp skipped reason=session_store_update_failed session=%s",
            sid,
        )


def rehydrate_session_index(app: "FastAPI", sid: str) -> dict[str, Any]:
    """Read back a session's stored badge index (badges only), ``{}`` if none."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return {}
    session = store.get(sid)
    if session is None:
        return {}
    index = session.metadata.get("artifacts")
    return dict(index) if isinstance(index, dict) else {}


# --------------------------------------------------------------------------- #
# Boot fold — rebuild the registry from ARC _events (fallback: JSONL trace)
# --------------------------------------------------------------------------- #


def rebuild_registry_at_boot(app: "FastAPI") -> ArtifactRegistry:
    """Rebuild ``app.state.artifact_registry`` from the durable event log at boot.

    Fold source precedence (owner decision #966): ARC ``_events`` first; JSONL
    durable trace as the fallback; when NEITHER is reachable, the registry boots
    empty and records a typed ``capture_released`` reason (never a silent empty).
    """
    registry = ArtifactRegistry()
    app.state.artifact_registry = registry

    folded = _fold_from_arc(app, registry)
    if folded is None:
        folded = _fold_from_jsonl(app, registry)
    if folded is None:
        registry.capture_released = {
            "reason": "capture_released",
            "detail": "no ARC _events and no durable JSONL trace reachable at boot",
        }
        logger.warning(
            "artifact registry boot fold skipped reason=capture_released "
            "detail=no_arc_events_no_jsonl_trace"
        )
    else:
        logger.info(
            "artifact registry boot fold source=%s records=%d conflicts=%d",
            folded,
            registry.count(),
            len(registry.fold_conflicts),
        )
    return registry


def _fold_from_arc(app: "FastAPI", registry: ArtifactRegistry) -> Optional[str]:
    """Fold artifact events from ARC's persisted ``_events`` log; ``None`` if absent."""
    from clio_agent.gact.runtime.globals import _PROCESS_ARC  # noqa: PLC0415

    arc = getattr(app.state, "arc", None) or _PROCESS_ARC
    observer = getattr(arc, "_live", None) or getattr(arc, "live", None)
    reader = getattr(observer, "iter_event_contents", None)
    if reader is None:
        return None
    found_any = False
    for content in reader():
        if str(content.get("event_type") or "") != ARTIFACT_CREATED_EVENT:
            continue
        payload = content.get("payload")
        if isinstance(payload, dict):
            registry.fold_payload(payload)
            found_any = True
    return "arc_events" if found_any else None


def _fold_from_jsonl(app: "FastAPI", registry: ArtifactRegistry) -> Optional[str]:
    """Fold artifact events from the durable JSONL traces; ``None`` if none exist."""
    root = _trace_dir(app)
    if root is None or not root.exists():
        return None
    import json  # noqa: PLC0415

    found_any = False
    for path in sorted(root.glob("*.semantic.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "artifact boot fold skipped a trace file reason=unreadable path=%s", path
            )
            continue
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if str(obj.get("event_type") or "") != ARTIFACT_CREATED_EVENT:
                continue
            payload = obj.get("payload")
            if isinstance(payload, dict):
                # The durable trace carries event_id at top level; thread it in.
                if not payload.get("event_id") and obj.get("event_id"):
                    payload = {**payload, "event_id": str(obj["event_id"])}
                registry.fold_payload(payload)
                found_any = True
    return "jsonl_trace" if found_any else None


def _trace_dir(app: "FastAPI") -> Optional[Path]:
    """Resolve the durable-trace directory the file backend writes into."""
    backend = getattr(app.state, "semantic_trace_backend", None)
    path = getattr(backend, "path", None)
    if isinstance(path, Path):
        return path if path.is_dir() or not path.suffix else path.parent
    raw = str(path) if path else ""
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.suffix == "" else candidate.parent


__all__ = [
    "ARTIFACT_CREATED_EVENT",
    "ArtifactRegistry",
    "FoldResult",
    "build_session_index",
    "get_registry",
    "patch_session_index",
    "rebuild_registry_at_boot",
    "rehydrate_session_index",
]
