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
``fold_conflict``. As of S2 (#968) ``artifact.created`` is on the SSE UI wire
(``SSE_UI_EVENT_TYPES``) and mints emit it at ``semantic`` detail; the durable
fold source is unchanged (capture ignores ``detail_level``).
"""

from __future__ import annotations

import asyncio
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
#: The version-chain + alias atoms of the ``artifact.*`` family (SSE-served, #968).
#: Emit sites land in S4 (#970); the names are pinned here so the SSE allow-list and
#: SPEC §7.6 co-edit reference one source. ``artifact.used`` /
#: ``artifact.transform.recorded`` are deliberately absent — they stay trace-only.
ARTIFACT_VERSION_ADDED_EVENT = "artifact.version.added"
ARTIFACT_ALIAS_MOVED_EVENT = "artifact.alias.moved"

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


@dataclass
class MintOutcome:
    """Typed outcome of an atomic :meth:`ArtifactRegistry.mint`.

    ``created`` is ``True`` when a genuinely new version was assigned and appended;
    ``False`` when the mint deduplicated onto an existing byte-identical version
    (W&B ``same name + same sha256`` dedup, owner decision #966.3) — the caller
    then emits NOTHING (the no-op is at the mint, not merely the fold). ``version``
    is always the operative version (the new one, or the deduped-onto existing).
    """

    version: ArtifactVersion
    created: bool
    reason: str = ""


class RegistryFoldOnLoopError(RuntimeError):
    """Raised when a boot fold would run synchronously on the asyncio event loop.

    The boot fold performs unbounded synchronous file / ARC I/O; running it on the
    loop thread would stall every in-flight SSE stream and session (the Campaign-1
    liveness lesson). The async mint seams (diffs/apply, pack-declared finalize)
    MUST offload the mint to a worker thread (``asyncio.to_thread``); this typed
    error is the backstop that makes an un-offloaded on-loop first access loud
    rather than a silent stall.
    """


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

    # ---- mint (atomic version assignment) ----------------------------------

    def mint(
        self,
        *,
        workspace_id: str,
        name: str,
        event_id: str,
        kind: ArtifactKind,
        custody: Custody,
        mechanism: Mechanism,
        evidence: IdentityEvidence,
        producer: dict[str, Any],
        path: str,
        created_at: str,
        annotation: str,
    ) -> MintOutcome:
        """Atomically dedup-or-assign the next version for ``(workspace_id, name)``.

        The single read-modify-write that assigns a version number runs under ONE
        lock acquisition (owner decision, finding [3/10] — no TOCTOU across two
        locks): consult the W&B same-sha dedup (finding [1/6] — content already in
        the chain → return the existing version, ``created=False``), else assign
        ``next_version_number()``, build the immutable version, append it, and mark
        ``event_id`` seen so the durable event's boot replay is a ``duplicate_event_id``
        no-op. Never emits — the caller emits the ``artifact.created`` event only
        when ``created`` is ``True``.
        """
        with self._lock:
            key = (workspace_id, name)
            record = self._records.get(key)
            if record is None:
                record = ArtifactRecord(workspace_id=workspace_id, name=name)
                self._records[key] = record

            # W&B dedup: same (ws, name) + same content sha256 -> no new version.
            # A stat-pinned version (sha256 is None) never dedups — identity unknown.
            sha = evidence.sha256
            if sha:
                deduped = record.version_for_sha(sha)
                if deduped is not None:
                    return MintOutcome(version=deduped, created=False, reason="same_sha_dedup")

            version = ArtifactVersion(
                version=record.next_version_number(),
                kind=kind,
                custody=custody,
                mechanism=mechanism,
                evidence=evidence,
                producer=dict(producer),
                path=path,
                created_at=created_at,
                annotation=annotation,
            )
            record.add_version(version)
            if event_id:
                self._seen_event_ids.add(event_id)
            return MintOutcome(version=version, created=True, reason="")

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

    def get_by_artifact_id(
        self, artifact_id: str
    ) -> Optional[tuple[ArtifactRecord, ArtifactVersion]]:
        """Resolve a version by its relay ``artifact_id`` (``artifact_<hex>``).

        Returns the ``(record, version)`` pair whose version carries that id, or
        ``None`` when no version matches. Each version's ``artifact_id`` is unique
        (one per immutable version — owner decision #966.3), so the first match is
        the only match. Linear over the chains; the fleet is bounded and this is a
        by-id lookup route, not a hot loop.
        """
        if not artifact_id:
            return None
        with self._lock:
            for record in self._records.values():
                for version in record.versions:
                    if version.artifact_id == artifact_id:
                        return (record, version)
        return None


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


#: Guards the lazy first-access rebuild so two concurrent first-accessors build
#: ONE registry, not two (finding [7]/[13] — the check-then-set race). Module
#: scope: there is one registry per app, installed on ``app.state``.
_REGISTRY_INIT_LOCK = threading.Lock()


def get_registry(app: "FastAPI") -> ArtifactRegistry:
    """Return the app's artifact registry, rebuilding it from the log on first access.

    The projection rebuilds LAZILY (RULE 4 / #737): the first consumer — a mint
    or a query — triggers :func:`rebuild_registry_at_boot`, which folds the durable
    ``artifact.created`` events (ARC ``_events`` UNION the JSONL trace; neither
    reachable → typed ``capture_released``). This keeps the boot seam out of the
    ``build_app`` god file while still rebuilding once, before first use.

    Thread-safe via double-checked locking (finding [7]/[13]): concurrent first
    accessors share the single installed instance instead of each building — and
    overwriting — their own. Loop-safe (finding [9]): the rebuild does unbounded
    synchronous I/O, so a first access ON the event loop raises the typed
    :class:`RegistryFoldOnLoopError` — the async mint seams offload to a worker
    thread; a built registry (the common case) is returned without touching the
    lock or the loop check.
    """
    registry = getattr(app.state, "artifact_registry", None)
    if registry is not None:
        return registry
    # First access — a rebuild is required. It must never run on the loop thread.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        on_loop = False
    else:
        on_loop = True
    if on_loop:
        raise RegistryFoldOnLoopError(
            "artifact registry boot fold would run on the asyncio event loop; the "
            "calling seam must offload the mint to a worker thread (asyncio.to_thread)"
        )
    with _REGISTRY_INIT_LOCK:
        # Double-check under the lock: a racing first-accessor may have built it.
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


@dataclass(frozen=True)
class _SourceFold:
    """Reachability + fold outcome for one boot-fold source.

    ``reachable`` distinguishes a source that was READ (present + readable, whether
    or not it held any artifact events) from one that was ABSENT or UNREADABLE.
    The empty-vs-unknown distinction (finding [11]): ``capture_released`` fires
    only when NEITHER source was reachable — a reachable-but-empty source is a
    clean empty registry, never a degrade.
    """

    reachable: bool
    folded_any: bool


def rebuild_registry_at_boot(app: "FastAPI") -> ArtifactRegistry:
    """Rebuild ``app.state.artifact_registry`` from the durable event log at boot.

    UNION-folds BOTH fold sources (owner decision #966.8, finding [2]): ARC
    ``_events`` AND the durable JSONL trace. The fold is idempotent (``event_id``
    dedup + same-sha no-op + keep-first), so folding both is safe and recovers
    deleted-session history the live ARC log no longer holds. Only when NEITHER
    source is reachable does the registry boot empty and record a typed
    ``capture_released`` reason (finding [11] — a reachable-but-empty source is a
    clean empty registry, not a degrade). ``app.state`` is assigned only after the
    fold completes, so a concurrent reader never sees a half-built projection.
    """
    registry = ArtifactRegistry()

    arc_fold = _fold_from_arc(app, registry)
    jsonl_fold = _fold_from_jsonl(app, registry)

    if not arc_fold.reachable and not jsonl_fold.reachable:
        registry.capture_released = {
            "reason": "capture_released",
            "detail": "neither ARC _events nor the durable JSONL trace was reachable at boot",
        }
        logger.warning(
            "artifact registry boot fold skipped reason=capture_released "
            "detail=no_reachable_fold_source"
        )
    else:
        logger.info(
            "artifact registry boot fold arc_reachable=%s jsonl_reachable=%s records=%d conflicts=%d",
            arc_fold.reachable,
            jsonl_fold.reachable,
            registry.count(),
            len(registry.fold_conflicts),
        )
    app.state.artifact_registry = registry
    return registry


def _fold_from_arc(app: "FastAPI", registry: ArtifactRegistry) -> _SourceFold:
    """Fold artifact events from ARC's persisted ``_events`` log.

    Returns ``reachable=False`` when ARC exposes no ``iter_event_contents`` reader
    (absent) or the reader raises mid-iteration (configured but unreadable);
    ``reachable=True`` when the log was read to completion, whether or not it held
    any artifact events.
    """
    from clio_agent.gact.runtime.globals import _PROCESS_ARC  # noqa: PLC0415

    arc = getattr(app.state, "arc", None) or _PROCESS_ARC
    observer = getattr(arc, "_live", None) or getattr(arc, "live", None)
    reader = getattr(observer, "iter_event_contents", None)
    if reader is None:
        return _SourceFold(reachable=False, folded_any=False)
    folded_any = False
    try:
        for content in reader():
            if not isinstance(content, dict):
                continue
            if str(content.get("event_type") or "") != ARTIFACT_CREATED_EVENT:
                continue
            payload = content.get("payload")
            if isinstance(payload, dict):
                registry.fold_payload(payload)
                folded_any = True
    except Exception:  # noqa: BLE001 — a configured-but-unreadable source is unreachable
        logger.warning(
            "artifact boot fold ARC source unreadable reason=arc_iter_failed folded_any=%s",
            folded_any,
        )
        return _SourceFold(reachable=False, folded_any=folded_any)
    return _SourceFold(reachable=True, folded_any=folded_any)


def _fold_from_jsonl(app: "FastAPI", registry: ArtifactRegistry) -> _SourceFold:
    """Fold artifact events from the durable JSONL traces.

    Streamed line-by-line with a cheap substring pre-filter (finding [4]): a line
    that cannot contain an ``artifact.created`` event is skipped BEFORE
    ``json.loads`` and the whole file is never read into memory, so a multi-GB
    trace dominated by non-artifact events costs ~one decode per artifact line.
    Returns ``reachable=False`` only when the trace directory is absent; a present
    directory with no (or no artifact-bearing) traces is ``reachable=True``.
    """
    root = _trace_dir(app)
    if root is None or not root.exists():
        return _SourceFold(reachable=False, folded_any=False)
    import json  # noqa: PLC0415

    folded_any = False
    for path in sorted(root.glob("*.semantic.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                for raw in handle:
                    # Cheap pre-filter: skip lines that cannot be an artifact event
                    # before paying json.loads (finding [4] — >99% of trace lines).
                    if ARTIFACT_CREATED_EVENT not in raw:
                        continue
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
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
                        folded_any = True
        except OSError:
            logger.warning(
                "artifact boot fold skipped a trace file reason=unreadable path=%s", path
            )
            continue
    return _SourceFold(reachable=True, folded_any=folded_any)


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
    "ARTIFACT_ALIAS_MOVED_EVENT",
    "ARTIFACT_CREATED_EVENT",
    "ARTIFACT_VERSION_ADDED_EVENT",
    "ArtifactRegistry",
    "FoldResult",
    "MintOutcome",
    "RegistryFoldOnLoopError",
    "build_session_index",
    "get_registry",
    "patch_session_index",
    "rebuild_registry_at_boot",
    "rehydrate_session_index",
]
