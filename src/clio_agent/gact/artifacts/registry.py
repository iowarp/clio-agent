"""Artifact registry — fold semantics, minting, queries over a pluggable backend.

The authoritative capture is the ``artifact.*`` semantic-event log emitted via
``_emit_semantic_event`` (ARC ``_events`` + durable trace; RULE 4 / #737). The
:class:`ArtifactRegistry` on ``app.state.artifact_registry`` owns the DECISIONS
(fold idempotency, atomic version assignment, alias last-writer-wins); where their
results live is its backend (:mod:`~clio_agent.gact.artifacts.registry_backend`):
clio-core ``artifact_index`` records served by point gets behind a bounded cache
(:mod:`~clio_agent.gact.artifacts.registry_store` — the process is stateless, boot
folds nothing), or, only when no clio-core record store exists, the in-process full
fold. A SMALL bounded SessionStore ``metadata`` patch is a fast badge index only.

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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Custody,
    IdentityEvidence,
    InvalidAliasError,
    Mechanism,
    alias_rejection_reason,
)
from clio_agent.gact.artifacts.registry_backend import MemoryBackend, PendingWrite
from clio_agent.gact.artifacts.registry_events import (
    _artifact_event_from_payload,
    _ArtifactEvent,
    _version_from_event,
)
from clio_agent.gact.artifacts.registry_index import (
    build_session_index,
    patch_session_index,
    rehydrate_session_index,
)
from clio_agent.gact.artifacts.versions import VersionAction, decide_version

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

ARTIFACT_CREATED_EVENT = "artifact.created"
#: The version-chain + alias atoms of the ``artifact.*`` family (SSE-served, #968),
#: emitted from S4 (#970). ``artifact.used`` is deliberately absent from the SSE
#: wire — it stays trace-only (:data:`~clio_agent.gact.semantic_events.SSE_TRACE_ONLY_EVENT_TYPES`
#: already reserves it). ``artifact.transform.recorded`` (S5 #971) IS folded (below)
#: to rebuild the transform/lineage index, but stays OFF the SSE wire (the S2 split).
ARTIFACT_VERSION_ADDED_EVENT = "artifact.version.added"
ARTIFACT_ALIAS_MOVED_EVENT = "artifact.alias.moved"
ARTIFACT_TRANSFORM_RECORDED_EVENT = "artifact.transform.recorded"
#: The use/custody atom (#1191): a same-sha DEDUP mint emits no new version/edge —
#: this is the honest "the deduping session used it" fact, folded (below) into a
#: per-session USE index only. Trace-only, like ``artifact.transform.recorded``.
ARTIFACT_USED_EVENT = "artifact.used"
#: A9 (#1176) dedup-enrichment atom — see :mod:`~clio_agent.gact.artifacts.dedup_enrichment`.
#: Trace-only, like ``artifact.used``; folded into its own side index below.
ARTIFACT_ENRICHED_EVENT = "artifact.enriched"

#: The event types the boot fold rebuilds the registry from: v1 creation (S1), v2+
#: revisions + alias moves (S4 #970), TransformRecords (S5 #971), the per-session
#: USE index (#1191), and the dedup-enrichment index (A9, #1176). ``.proposed`` is
#: NOT here — transform/use/enriched events rebuild their own indexes only.
_FOLD_EVENT_TYPES: frozenset[str] = frozenset(
    {
        ARTIFACT_CREATED_EVENT,
        ARTIFACT_VERSION_ADDED_EVENT,
        ARTIFACT_ALIAS_MOVED_EVENT,
        ARTIFACT_TRANSFORM_RECORDED_EVENT,
        ARTIFACT_USED_EVENT,
        ARTIFACT_ENRICHED_EVENT,
    }
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
    """The artifact projection's decisions over a backend (in-memory or clio-core).

    Thread-safe (a single lock guards every read-modify-write). The fold is
    idempotent and replay-order-tolerant: it dedupes by ``event_id`` then
    ``(workspace_id, name, version)`` and keeps the first content-hash on a conflict.
    It never reads the SessionStore badge index — that index is a projection OF this
    registry, not a source FOR it. ``backend`` defaults to the in-memory projection
    (a fold target); the boot wires the clio-core record backend when it exists.
    """

    def __init__(self, backend: Optional[Any] = None) -> None:
        self._lock = threading.Lock()
        self._backend: Any = backend if backend is not None else MemoryBackend()
        self.fold_conflicts: list[dict[str, Any]] = []
        #: Typed reason recorded when boot found no fold source at all.
        self.capture_released: Optional[dict[str, Any]] = None

    @property
    def _seen_event_ids(self) -> Any:
        """Event ids already applied (fold dedup; bounded on the clio-core backend)."""
        return self._backend.seen_event_ids

    def confirm_event(self, event_id: str) -> None:
        """The ``artifact.*`` event ``event_id`` is recorded — clear its pending marks."""
        with self._lock:
            self._backend.confirm(event_id)

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
        if event_type == ARTIFACT_TRANSFORM_RECORDED_EVENT:
            return self.fold_transform_recorded(payload)
        if event_type == ARTIFACT_USED_EVENT:
            return self.fold_artifact_used(payload)
        if event_type == ARTIFACT_ENRICHED_EVENT:
            return self.fold_artifact_enriched(payload)
        if event_type in _FOLD_EVENT_TYPES:
            return self.fold_payload(payload)
        return FoldResult(applied=False, reason="unfolded_type")

    def fold_transform_recorded(self, payload: dict[str, Any]) -> FoldResult:
        """Fold one ``artifact.transform.recorded`` payload (S5 #971).

        Idempotent: a duplicate ``event_id`` is a no-op; a second record for a
        ``call_id`` already seen keeps the FIRST (a coarse transform is one call —
        a re-emit is a replay, not a distinct activity). A malformed payload (no
        ``call_id``) is dropped with a typed reason.
        """
        from clio_agent.gact.artifacts.transforms import transform_from_payload  # noqa: PLC0415

        record = transform_from_payload(payload)
        if record is None:
            return FoldResult(applied=False, reason="malformed")
        event_id = str(payload.get("event_id") or record.event_id or "")
        with self._lock:
            if event_id and event_id in self._seen_event_ids:
                return FoldResult(applied=False, reason="duplicate_event_id")
            if event_id:
                self._seen_event_ids.add(event_id)
            if self._backend.transform(record.call_id) is not None:
                return FoldResult(applied=False, reason="duplicate_call_id")
            self._backend.add_transform(record)
            return FoldResult(applied=True, reason="")

    def record_transform(self, record: Any) -> bool:
        """Store a live-minted TransformRecord idempotently (S5 #971).

        Called BEFORE its ``artifact.transform.recorded`` event is emitted (the index
        record lands first, pending until the event is recorded). Marks the record's
        ``event_id`` seen so a replay of that event is a ``duplicate_event_id`` no-op,
        exactly like :meth:`mint`. Returns whether it was newly stored (``False``
        when the ``call_id`` was already recorded).
        """
        with self._lock:
            if record.event_id:
                self._seen_event_ids.add(record.event_id)
            if self._backend.transform(record.call_id) is not None:
                return False
            pending = PendingWrite(str(record.event_id or ""), str(record.session_id or ""))
            self._backend.add_transform(record, pending=pending)
            return True

    def get_transform(self, call_id: str) -> Optional[Any]:
        """Return the TransformRecord for ``call_id`` (the activity id), or ``None``."""
        with self._lock:
            return self._backend.transform(call_id)

    def transforms_for_session(self, session_id: str) -> list[Any]:
        """Every TransformRecord produced in ``session_id`` (chronological by call)."""
        with self._lock:
            return self._backend.session_transforms(session_id)

    def transforms_generating(self, artifact_id: str) -> list[Any]:
        """TransformRecords with a generated edge on ``artifact_id`` (record order)."""
        with self._lock:
            return self._backend.generating(artifact_id)

    def transforms_using(self, artifact_id: str) -> list[Any]:
        """TransformRecords with a used edge on ``artifact_id`` (record order)."""
        with self._lock:
            return self._backend.using(artifact_id)

    def record_artifact_used(
        self, session_id: str, artifact_id: str, *, event_id: str = ""
    ) -> bool:
        """Record a live same-sha-dedup USE for ``session_id`` (#1191, materialize half —
        paired with the durable :func:`~clio_agent.gact.artifacts.versions.emit_artifact_used`
        emit, which follows). Idempotent by ``event_id``; returns whether newly recorded."""
        return self._record_used(session_id, artifact_id, event_id, live=True)

    def _record_used(self, session_id: str, artifact_id: str, event_id: str, *, live: bool) -> bool:
        if not session_id or not artifact_id:
            return False
        with self._lock:
            if event_id:
                if event_id in self._seen_event_ids:
                    return False
                self._seen_event_ids.add(event_id)
            if artifact_id in self._backend.used(session_id):
                return False
            pending = PendingWrite(event_id, session_id) if live else None
            self._backend.add_used(session_id, artifact_id, pending=pending)
            return True

    def fold_artifact_used(self, payload: dict[str, Any]) -> FoldResult:
        """Fold one ``artifact.used`` payload (#1191) into the SAME index the live mint
        path writes — malformed (no session/artifact id) is dropped with a typed reason."""
        session_id = str(payload.get("session_id") or "")
        artifact_id = str(payload.get("artifact_id") or "")
        event_id = str(payload.get("event_id") or "")
        if not session_id or not artifact_id:
            return FoldResult(applied=False, reason="malformed")
        if not self._record_used(session_id, artifact_id, event_id, live=False):
            return FoldResult(applied=False, reason="duplicate_event_id")
        return FoldResult(applied=True, reason="")

    def used_artifact_ids_for_session(self, session_id: str) -> set[str]:
        """Relay ``artifact_id``s ``session_id`` USED via dedup (#1191, never produced)."""
        with self._lock:
            return self._backend.used(session_id)

    def record_artifact_enrichment(
        self, artifact_id: str, *, annotation: str = "", event_id: str = "", session_id: str = ""
    ) -> str:
        """Attach a SUPPLEMENTAL annotation to ``artifact_id`` (A9, #1176).

        Decision logic lives in the owner module
        :func:`~clio_agent.gact.artifacts.dedup_enrichment.decide_enrichment`
        (no-accretion); returns one of its typed reasons, never a silent drop. A live
        call passes the emitting ``session_id`` (its index write is pending until the
        ``artifact.enriched`` event is recorded); a fold passes none.
        """
        from clio_agent.gact.artifacts.dedup_enrichment import decide_enrichment  # noqa: PLC0415

        pending = PendingWrite(event_id, session_id) if session_id else None
        with self._lock:
            return decide_enrichment(self, artifact_id, annotation, event_id, pending=pending)

    def fold_artifact_enriched(self, payload: dict[str, Any]) -> FoldResult:
        """Fold one ``artifact.enriched`` payload (A9, #1176) via
        :meth:`record_artifact_enrichment` — malformed (no artifact id) is a
        typed skip."""
        artifact_id = str(payload.get("artifact_id") or "")
        if not artifact_id:
            return FoldResult(applied=False, reason="malformed")
        reason = self.record_artifact_enrichment(
            artifact_id,
            annotation=str(payload.get("annotation") or ""),
            event_id=str(payload.get("event_id") or ""),
        )
        return FoldResult(applied=(reason == "merged"), reason=reason)

    def supplemental_annotation(self, artifact_id: str) -> str:
        """The dedup-enrichment annotation attached to ``artifact_id``, or ``""``."""
        with self._lock:
            return self._backend.enrichment(artifact_id)

    def all_transforms(self) -> list[Any]:
        """Every TransformRecord (in-memory projection only — the clio-core backend
        serves transforms by session / artifact id and raises a typed error here)."""
        with self._lock:
            return self._backend.all_transforms()

    def find_version_by_path(
        self,
        workspace_id: str,
        path: str,
        *,
        allowed_workspace_ids: Optional[set[str]] = None,
    ) -> Optional[tuple[ArtifactRecord, ArtifactVersion]]:
        """Resolve a registered version by its referenced ``path`` (S5 used-edge match).

        Returns the ``(record, version)`` whose ``version.path`` resolves to the same
        absolute path (``None`` when none does). Linear over the searched workspaces.

        ``allowed_workspace_ids`` (P3.1 #1038 — cross-job lineage bind) gates which
        workspaces a match may come from. ``None`` (default, drop-in for existing
        callers) → same-workspace-only: only versions under ``workspace_id``, HEAD
        (highest ``version``) wins. A set → the CROSS-JOB contributing set (every
        workspace sharing the current job's ``root_path``); a version whose ``ws in
        allowed_workspace_ids`` matches on absolute path equality, with a
        DETERMINISTIC cross-record tie-break — prefer an exact ``ws == workspace_id``
        match, else the newest by ``(created_at, ws, name, version)``, a total order
        independent of iteration order (flag #4), never the naive per-record HEAD-wins.
        """
        if not path:
            return None
        target = Path(str(path)).expanduser().resolve(strict=False)
        searched = (
            [workspace_id] if allowed_workspace_ids is None else sorted(allowed_workspace_ids)
        )
        with self._lock:
            best_key: Optional[tuple[Any, ...]] = None
            best: Optional[tuple[ArtifactRecord, ArtifactVersion]] = None
            for record in (r for ws in searched for r in self._backend.workspace_records(ws)):
                ws, name = record.workspace_id, record.name
                for version in record.versions:
                    if not version.path:
                        continue
                    try:
                        vpath = Path(version.path).expanduser().resolve(strict=False)
                    except OSError:
                        continue
                    if vpath != target:
                        continue
                    # Default: HEAD (highest version) wins. Cross-job: prefer local ws,
                    # else newest by (created_at, ws, name, version) — a deterministic
                    # total order, never iteration order (flag #4).
                    key: tuple[Any, ...] = (
                        (version.version,)
                        if allowed_workspace_ids is None
                        else (
                            ws == workspace_id,
                            version.created_at or "",
                            ws,
                            name,
                            version.version,
                        )
                    )
                    if best_key is None or key >= best_key:
                        best_key, best = key, (record, version)
            return best

    def _fold_event(self, event: _ArtifactEvent) -> FoldResult:
        with self._lock:
            if event.event_id and event.event_id in self._seen_event_ids:
                return FoldResult(applied=False, reason="duplicate_event_id")
            record = self._backend.ensure_record(event.workspace_id, event.name)

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
            self._backend.save_record(record, new_version=version)
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
            record = self._backend.ensure_record(ws, name)
            applied = self._apply_alias_move_locked(
                record, alias=alias, to_version=to_version, at=at, event_id=event_id
            )
            if event_id:
                self._seen_event_ids.add(event_id)
            if not applied:
                return FoldResult(applied=False, reason="stale_alias_move")
            return FoldResult(applied=True, reason="")

    def _apply_alias_move_locked(
        self,
        record: ArtifactRecord,
        *,
        alias: str,
        to_version: int,
        at: str,
        event_id: str,
        pending: Optional[PendingWrite] = None,
    ) -> bool:
        """Apply one alias move under the ``(at, event_id)`` last-writer-wins order.

        The ONE comparator shared by the boot fold (:meth:`fold_alias_moved`) and the
        live route (:meth:`move_alias`) — finding [5]: a live stale move (older ``(at,
        event_id)`` than the recorded winner) is refused exactly as the fold refuses a
        replayed one, so a rebuild converges on the live state. Returns whether it was
        applied (``False`` == stale no-op). Caller holds ``self._lock``; ``latest`` is
        derived from the head, so its pointer is not stored but its key IS recorded.
        """
        move_key = (at, event_id)
        best = self._backend.alias_key(record, alias)
        if best is not None and move_key <= best:
            return False
        undo = {"op": "alias", "alias": alias, "prev": record.aliases.get(alias), "prev_key": best}
        self._backend.set_alias_key(record, alias, move_key)
        if alias != "latest":
            record.aliases[alias] = to_version
        self._backend.save_record(record, pending=pending, undo=undo)
        return True

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
        not_ingested_size: Optional[int] = None,
        session_id: str = "",
    ) -> MintOutcome:
        """Atomically decide-and-append the next version for ``(workspace_id, name)``.

        The single read-modify-write runs under ONE lock (finding [3/10] — no TOCTOU).
        The version DECISION is delegated to the one decision point
        (:func:`~clio_agent.gact.artifacts.versions.decide_version`) — no dedup /
        version-number / revision logic lives here — which, on the locked chain
        snapshot, returns a dedup (``created=False``) or a new version carrying its
        number, the ``wasRevisionOf`` edge, and any kind / custody-gap markers. This
        only MATERIALIZES that decision, persists it (pending under ``event_id`` until
        the event emitted into ``session_id`` is recorded), and marks ``event_id``
        seen. Never emits; the caller emits ``artifact.created`` (v1) /
        ``artifact.version.added`` (v2+) when ``created``. ``producing`` /
        ``lease_clean`` forward to the decision point's drift path.
        """
        with self._lock:
            record = self._backend.ensure_record(workspace_id, name)
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
                not_ingested_size=not_ingested_size,
            )
            record.add_version(version)
            sid = session_id or str(producer.get("session_id") or "")
            self._backend.save_record(
                record, new_version=version, pending=PendingWrite(event_id, sid)
            )
            if event_id:
                self._seen_event_ids.add(event_id)
            return MintOutcome(
                version=version,
                created=True,
                reason=decision.reason,
                action=decision.action.value,
            )

    def move_alias(
        self,
        workspace_id: str,
        name: str,
        *,
        alias: str,
        to_version: int,
        at: str,
        event_id: str,
        session_id: str = "",
    ) -> Optional[tuple[Optional[int], int, bool]]:
        """Live alias move under the lock — ``(from_version, to_version, applied)``.

        Finding [7]: refuses a ``latest`` / ``vN`` alias with a typed
        :class:`InvalidAliasError` at the record layer (behind the route's own check).
        Finding [5]: decided by the SAME ``(at, event_id)`` comparator the fold uses
        (:meth:`_apply_alias_move_locked`), so a stale live move is a no-op
        (``applied=False``) exactly as the fold refuses it. ``None`` when the record or
        target version is missing; the emitted event (into ``session_id``) MUST carry
        this ``at`` + ``event_id``.
        """
        reason = alias_rejection_reason(alias)
        if reason:
            raise InvalidAliasError(f"alias {alias!r} is not a legal user alias", reason=reason)
        with self._lock:
            record = self._backend.record(workspace_id, name)
            if record is None:
                return None
            if not any(v.version == to_version for v in record.versions):
                return None
            from_version = record.aliases.get(alias)
            applied = self._apply_alias_move_locked(
                record,
                alias=alias,
                to_version=to_version,
                at=at,
                event_id=event_id,
                pending=PendingWrite(event_id, session_id),
            )
            return (from_version, to_version, applied)

    # ---- queries -----------------------------------------------------------

    def get(self, workspace_id: str, name: str) -> Optional[ArtifactRecord]:
        """Return the logical record for ``(workspace_id, name)``, or ``None``."""
        with self._lock:
            return self._backend.record(workspace_id, name)

    def list_for_workspace(self, workspace_id: str) -> list[ArtifactRecord]:
        """Every logical artifact in a workspace (creation order)."""
        with self._lock:
            return self._backend.workspace_records(workspace_id)

    def is_sha_alias_reachable(self, workspace_id: str, sha256: str) -> bool:
        """Whether any pinned alias in ``workspace_id`` currently targets ``sha256``.

        The cheap, lock-held re-check the CAS GC runs immediately before an unlink
        (finding [4] TOCTOU): a version minted AFTER the GC read its records snapshot
        auto-moves the ``latest`` alias onto its content, so an alias now targeting the
        blob means it is live and must NOT be evicted. Evaluated under the registry
        lock against the freshest chains, closing the snapshot-then-fold race.
        """
        if not sha256:
            return False
        with self._lock:
            for record in self._backend.workspace_records(workspace_id):
                by_number = {v.version: v for v in record.versions}
                for target in record.aliases.values():
                    version = by_number.get(target)
                    if version is not None and version.sha256 == sha256:
                        return True
        return False

    def count(self) -> int:
        """Total number of logical artifacts known."""
        with self._lock:
            return len(self._backend.all_records())

    def all_records(self) -> list[ArtifactRecord]:
        """A snapshot of every logical record (introspection / tests; walks every
        workspace — hot paths query by workspace, session or artifact id instead)."""
        with self._lock:
            return self._backend.all_records()

    def versions_produced_in(
        self, session_ids: Iterable[str]
    ) -> list[tuple[ArtifactRecord, ArtifactVersion]]:
        """Every ``(record, version)`` whose ``producer.session_id`` is in ``session_ids``."""
        with self._lock:
            return self._backend.produced_in(sorted(set(session_ids)))

    def get_by_artifact_id(
        self, artifact_id: str
    ) -> Optional[tuple[ArtifactRecord, ArtifactVersion]]:
        """Resolve a version by its relay ``artifact_id`` (``artifact_<hex>``).

        Returns the ``(record, version)`` pair whose version carries that id, or
        ``None`` when none matches. Each version's ``artifact_id`` is unique (one per
        immutable version — owner decision #966.3).
        """
        if not artifact_id:
            return None
        with self._lock:
            return self._backend.locate(artifact_id)


#: Guards the lazy first-access rebuild so two concurrent first-accessors build
#: ONE registry, not two (finding [7]/[13] — the check-then-set race). Module
#: scope: there is one registry per app, installed on ``app.state``.
_REGISTRY_INIT_LOCK = threading.Lock()


def _retry_boot_fold(app: "FastAPI") -> Optional[ArtifactRegistry]:
    """One-shot lazy refold after a ``capture_released`` boot.

    Observed live: the boot fold's ARC read raises during early boot
    (``arc_iter_failed``) while the same store serves reads minutes later, so
    the registry served empty for the whole process life. Off-loop callers
    refold inline and get the rebuilt registry; on-loop callers must not run
    the fold's synchronous native I/O, so the refold runs on a daemon thread
    and THIS reader still sees the empty registry — the next one gets the
    rebuilt projection.
    """
    from clio_agent.gact.artifacts import registry_boot  # noqa: PLC0415 — import cycle

    def _refold() -> None:
        try:
            rebuilt = registry_boot.rebuild_registry_at_boot(app)
            logger.info("artifact registry lazy refold completed records=%d", rebuilt.count())
        except Exception as exc:  # noqa: BLE001 — a failed retry keeps the typed state
            logger.warning("artifact registry lazy refold failed cause=%r", exc)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _refold()
        return getattr(app.state, "artifact_registry", None)
    threading.Thread(target=_refold, name="artifact-registry-refold", daemon=True).start()
    return None


def get_registry(app: "FastAPI") -> ArtifactRegistry:
    """Return the app's artifact registry, opening it on first access.

    The first consumer without a boot-installed registry triggers
    :func:`rebuild_registry_at_boot`: the clio-core record index when its migration
    marker exists (no fold), else the one-time migration fold (ARC ``_events`` UNION
    the JSONL trace; neither reachable → typed ``capture_released``). Thread-safe via
    double-checked locking
    (finding [7]/[13]): concurrent first accessors share the single installed
    instance. Loop-safe (finding [9]): the rebuild's synchronous I/O must not run on
    the event loop, so a first access there raises :class:`RegistryFoldOnLoopError` —
    the async seams offload to a worker thread; a built registry returns without the
    lock or loop check.
    """
    registry = getattr(app.state, "artifact_registry", None)
    if registry is not None:
        if (
            registry.capture_released is not None
            and registry.count() == 0
            and not getattr(registry, "_boot_retry_attempted", False)
        ):
            # The boot fold found NO reachable source (observed live: the ARC
            # reader raises during early boot while the same store serves
            # reads minutes later). One lazy refold on first access — ARC is
            # up by now — instead of serving an empty registry all process
            # long. One attempt only; a second failure keeps the typed
            # capture_released state.
            registry._boot_retry_attempted = True
            retried = _retry_boot_fold(app)
            if retried is not None:
                return retried
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


__all__ = [
    "ARTIFACT_ALIAS_MOVED_EVENT",
    "ARTIFACT_CREATED_EVENT",
    "ARTIFACT_ENRICHED_EVENT",
    "ARTIFACT_TRANSFORM_RECORDED_EVENT",
    "ARTIFACT_USED_EVENT",
    "ARTIFACT_VERSION_ADDED_EVENT",
    "ArtifactRegistry",
    "FoldResult",
    "InvalidAliasError",
    "MintOutcome",
    "RegistryFoldOnLoopError",
    "build_session_index",
    "get_registry",
    "patch_session_index",
    "rebuild_registry_at_boot",
    "rehydrate_session_index",
]

# Boot fold lives in its own owner module (no-accretion); re-exported here so the
# lazy first-access rebuild + existing ``from registry import rebuild_registry_at_boot``
# callers stay green. The bottom import is safe: ``ArtifactRegistry`` and
# ``_FOLD_EVENT_TYPES`` are defined above, so ``registry_boot`` imports cleanly.
from clio_agent.gact.artifacts.registry_boot import (  # noqa: E402,F401
    rebuild_registry_at_boot,
)
