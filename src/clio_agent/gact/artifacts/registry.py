"""In-memory artifact registry — projection, fold semantics, minting, boot rebuild.

Storage is a PROJECTION over the existing semantic-event log (RULE 4 / #737 —
owner decision #966.8), not a new store: the authoritative capture is
``artifact.*`` semantic events emitted via ``_emit_semantic_event`` (ARC
``_events`` + durable trace); the in-memory :class:`ArtifactRegistry` on
``app.state.artifact_registry`` rebuilds from the fold set on first access; a
SMALL bounded SessionStore ``metadata`` patch is a fast badge index only, NEVER
the rebuild source.

Fold idempotency (owner decision #966): dedupe by ``event_id`` then
``(workspace_id, name, version)``; a same-sha replay is a no-op; a conflicting sha
keeps the FIRST + records a typed ``fold_conflict``. S4 (#970) folds three event
types — ``artifact.created`` (v1), ``artifact.version.added`` (v2+, revision edge),
``artifact.alias.moved`` (aliases, last-writer-wins) — into the same projection.
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
from clio_agent.gact.artifacts.versions import VersionAction, decide_version

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

ARTIFACT_CREATED_EVENT = "artifact.created"
#: The version-chain + alias atoms of the ``artifact.*`` family (SSE-served, #968),
#: emitted from S4 (#970). ``artifact.used`` / ``artifact.transform.recorded`` are
#: deliberately absent — they stay trace-only.
ARTIFACT_VERSION_ADDED_EVENT = "artifact.version.added"
ARTIFACT_ALIAS_MOVED_EVENT = "artifact.alias.moved"

#: The event types the boot fold rebuilds the registry from (S4 #970): v1 creation,
#: v2+ revisions, and alias moves. ``artifact.used`` / ``.transform.recorded`` /
#: ``.proposed`` are NOT here — provenance/proposal substrate the chain never folds.
_FOLD_EVENT_TYPES: frozenset[str] = frozenset(
    {ARTIFACT_CREATED_EVENT, ARTIFACT_VERSION_ADDED_EVENT, ARTIFACT_ALIAS_MOVED_EVENT}
)

# Bounded SessionStore badge index: at most this many named artifacts per session
# before it truncates (``names_truncated=True``). Badges only; never a rebuild source.
_SESSION_INDEX_NAME_CAP = 64


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

    ``created`` is ``True`` when a genuinely new version was appended; ``False`` on a
    W&B same-sha dedup (owner decision #966.3) — the caller then emits NOTHING (the
    no-op is at the mint, not merely the fold). ``version`` is always the operative
    version (the new one, or the deduped-onto existing).
    """

    version: ArtifactVersion
    created: bool
    reason: str = ""
    #: The version-decision action (``new_version`` / ``dedup`` / ``relink`` / ``gap``
    #: — :class:`~clio_agent.gact.artifacts.versions.VersionAction`); lets a caller
    #: read whether a custody gap was recorded.
    action: str = ""


class RegistryFoldOnLoopError(RuntimeError):
    """Raised when a boot fold would run synchronously on the asyncio event loop.

    The boot fold performs unbounded synchronous file / ARC I/O; on the loop thread
    it would stall every in-flight SSE stream (the Campaign-1 liveness lesson). The
    async mint seams MUST offload to a worker thread (``asyncio.to_thread``); this
    typed error is the backstop that makes an un-offloaded first access loud.
    """


class ArtifactRegistry:
    """In-memory projection of the artifact event log, rebuilt at boot.

    Thread-safe (a single lock guards the chains). The fold is idempotent and
    replay-order-tolerant: it dedupes by ``event_id`` then ``(workspace_id, name,
    version)`` and keeps the first content-hash on a conflict. It never reads the
    SessionStore badge index — that index is a projection OF this registry, not a
    source FOR it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], ArtifactRecord] = {}
        self._seen_event_ids: set[str] = set()
        self.fold_conflicts: list[dict[str, Any]] = []
        #: Typed reason recorded when boot found no fold source at all.
        self.capture_released: Optional[dict[str, Any]] = None
        #: Last-writer-wins bookkeeping for the alias fold (S4 #970): maps
        #: ``(workspace_id, name, alias)`` → the winning move's ``(at, event_id)``, so
        #: an order-shuffled replay rebuilds the identical alias map (greatest wins).
        self._alias_move_keys: dict[tuple[str, str, str], tuple[str, str]] = {}

    # ---- fold --------------------------------------------------------------

    def fold_payload(self, payload: dict[str, Any]) -> FoldResult:
        """Fold one version payload (``artifact.created`` / ``.version.added``).

        Both carry a version record (v1 vs a v2+ revision); the parser reads the
        revision-edge fields when present, so one path builds both. Alias moves fold
        via :meth:`fold_alias_moved`.
        """
        event = _artifact_event_from_payload(payload)
        if event is None:
            return FoldResult(applied=False, reason="malformed")
        return self._fold_event(event)

    def fold_event_by_type(self, event_type: str, payload: dict[str, Any]) -> FoldResult:
        """Dispatch a fold by event type (the boot-fold seam's single entry).

        Alias moves fold last-writer-wins via :meth:`fold_alias_moved`; the two version
        events via :meth:`fold_payload`. An unknown type is a typed ``unfolded_type``.
        """
        if event_type == ARTIFACT_ALIAS_MOVED_EVENT:
            return self.fold_alias_moved(payload)
        if event_type in _FOLD_EVENT_TYPES:
            return self.fold_payload(payload)
        return FoldResult(applied=False, reason="unfolded_type")

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

    def fold_alias_moved(self, payload: dict[str, Any]) -> FoldResult:
        """Fold one ``artifact.alias.moved`` payload, last-writer-wins (S4 #970).

        Deterministic under replay in ANY order: the winner for an ``(workspace_id,
        name, alias)`` is the move with the greatest ``(at, event_id)`` total order, so
        an order-shuffled log rebuilds the identical alias map. A move that does not
        beat the recorded winner is a typed ``stale_alias_move`` no-op.
        """
        ws = str(payload.get("workspace_id") or "")
        name = str(payload.get("name") or "")
        alias = str(payload.get("alias") or "")
        event_id = str(payload.get("event_id") or "")
        if not name or not alias:
            return FoldResult(applied=False, reason="malformed")
        raw_to = payload.get("to_version")
        if raw_to is None:
            return FoldResult(applied=False, reason="malformed")
        try:
            to_version = int(raw_to)
        except (TypeError, ValueError):
            return FoldResult(applied=False, reason="malformed")
        at = str(payload.get("at") or "")
        with self._lock:
            if event_id and event_id in self._seen_event_ids:
                return FoldResult(applied=False, reason="duplicate_event_id")
            move_key = (at, event_id)
            alias_id = (ws, name, alias)
            best = self._alias_move_keys.get(alias_id)
            if best is not None and move_key <= best:
                if event_id:
                    self._seen_event_ids.add(event_id)
                return FoldResult(applied=False, reason="stale_alias_move")
            record = self._records.get((ws, name))
            if record is None:
                record = ArtifactRecord(workspace_id=ws, name=name)
                self._records[(ws, name)] = record
            self._alias_move_keys[alias_id] = move_key
            # ``latest`` is auto-maintained to the head by add_version; deriving it
            # from the chain (not a possibly-lossy move log) keeps latest == head.
            if alias != "latest":
                record.aliases[alias] = to_version
            if event_id:
                self._seen_event_ids.add(event_id)
            return FoldResult(applied=True, reason="")

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
        producing: bool = True,
        lease_clean: bool = False,
    ) -> MintOutcome:
        """Atomically decide-and-append the next version for ``(workspace_id, name)``.

        The single read-modify-write runs under ONE lock (finding [3/10] — no TOCTOU).
        The version DECISION is delegated to the one decision point
        (:func:`~clio_agent.gact.artifacts.versions.decide_version`) — no dedup /
        version-number / revision logic lives here — which, on the locked chain
        snapshot, returns a dedup (``created=False``) or a new version carrying its
        number, the ``wasRevisionOf`` edge, and any kind / custody-gap markers. This
        only MATERIALIZES that decision, appends it, and marks ``event_id`` seen so a
        boot replay is a ``duplicate_event_id`` no-op. Never emits; the caller emits
        ``artifact.created`` (v1) / ``artifact.version.added`` (v2+) when ``created``.
        ``producing`` / ``lease_clean`` forward to the decision point's drift path.
        """
        with self._lock:
            key = (workspace_id, name)
            record = self._records.get(key)
            if record is None:
                record = ArtifactRecord(workspace_id=workspace_id, name=name)
                self._records[key] = record

            decision = decide_version(
                record,
                sha256=evidence.sha256,
                requested_kind=kind,
                requested_mechanism=mechanism,
                producing=producing,
                lease_clean=lease_clean,
            )
            if decision.action is VersionAction.DEDUP:
                if event_id:
                    self._seen_event_ids.add(event_id)
                assert decision.deduped_onto is not None
                return MintOutcome(
                    version=decision.deduped_onto,
                    created=False,
                    reason=decision.reason,
                    action=decision.action.value,
                )

            version = ArtifactVersion(
                version=decision.version_number,
                kind=decision.kind,
                custody=custody,
                mechanism=decision.mechanism,
                evidence=evidence,
                producer=dict(producer),
                path=path,
                created_at=created_at,
                annotation=annotation,
                prior_version=decision.prior_version,
                prior_sha256=decision.prior_sha256,
                kind_warning=decision.kind_warning,
                custody_gap=decision.custody_gap,
            )
            record.add_version(version)
            if event_id:
                self._seen_event_ids.add(event_id)
            return MintOutcome(
                version=version,
                created=True,
                reason=decision.reason,
                action=decision.action.value,
            )

    def move_alias(
        self, workspace_id: str, name: str, *, alias: str, to_version: int
    ) -> Optional[tuple[Optional[int], int]]:
        """Live alias move under the lock — returns ``(from_version, to_version)``.

        Sets ``record.aliases[alias] = to_version``; the emitted ``artifact.alias.moved``
        event carries ``(at, event_id)`` so a boot replay converges on the same map.
        Returns ``None`` when the record or the target version is missing (the caller
        surfaces a typed 404). The reserved ``latest`` alias is guarded at the route.
        """
        with self._lock:
            record = self._records.get((workspace_id, name))
            if record is None:
                return None
            if not any(v.version == to_version for v in record.versions):
                return None
            from_version = record.aliases.get(alias)
            record.aliases[alias] = to_version
            return (from_version, to_version)

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
        ``None`` when none matches. Each version's ``artifact_id`` is unique (one per
        immutable version — owner decision #966.3). Linear over the bounded fleet.
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
        prior_version=event.prior_version,
        prior_sha256=event.prior_sha256,
        kind_warning=event.kind_warning,
        custody_gap=event.custody_gap,
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

    The projection rebuilds LAZILY (RULE 4 / #737): the first consumer triggers
    :func:`rebuild_registry_at_boot` (ARC ``_events`` UNION the JSONL trace; neither
    reachable → typed ``capture_released``). Thread-safe via double-checked locking
    (finding [7]/[13]): concurrent first accessors share the single installed
    instance. Loop-safe (finding [9]): the rebuild's synchronous I/O must not run on
    the event loop, so a first access there raises :class:`RegistryFoldOnLoopError` —
    the async seams offload to a worker thread; a built registry returns without the
    lock or loop check.
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

    Shape ``{count, names: {name: {v, id, kind}}, names_truncated}`` — small, bounded
    (:data:`_SESSION_INDEX_NAME_CAP` names). The full set always rebuilds from the
    event log; this is never read back as a source (owner decision #966.8 / #966.4).
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
            "artifact session-index stamp skipped reason=store_update_failed sid=%s", sid
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

    UNION-folds BOTH sources (owner decision #966.8, finding [2]): ARC ``_events``
    AND the durable JSONL trace. The fold is idempotent (``event_id`` dedup + same-sha
    no-op + keep-first), so folding both is safe and recovers deleted-session history.
    Only when NEITHER source is reachable does the registry boot empty with a typed
    ``capture_released`` reason (finding [11] — a reachable-but-empty source is a clean
    empty registry, not a degrade). ``app.state`` is assigned only after the fold
    completes, so a concurrent reader never sees a half-built projection.
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
            event_type = str(content.get("event_type") or "")
            if event_type not in _FOLD_EVENT_TYPES:
                continue
            payload = content.get("payload")
            if isinstance(payload, dict):
                registry.fold_event_by_type(event_type, payload)
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

    Streamed line-by-line with a cheap ``artifact.`` substring pre-filter (finding
    [4]): a line that cannot hold a fold event is skipped BEFORE ``json.loads`` and
    the file is never read whole, so a multi-GB non-artifact trace costs ~one decode
    per artifact line. ``reachable=False`` only when the trace directory is absent.
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
                    # Cheap pre-filter (finding [4]): every fold event type shares the
                    # ``artifact.`` prefix, so a non-artifact line is rejected pre-decode.
                    if "artifact." not in raw:
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
                    event_type = str(obj.get("event_type") or "")
                    if event_type not in _FOLD_EVENT_TYPES:
                        continue
                    payload = obj.get("payload")
                    if isinstance(payload, dict):
                        # The durable trace carries event_id at top level; thread it in.
                        if not payload.get("event_id") and obj.get("event_id"):
                            payload = {**payload, "event_id": str(obj["event_id"])}
                        registry.fold_event_by_type(event_type, payload)
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
