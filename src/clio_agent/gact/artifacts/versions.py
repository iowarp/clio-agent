"""The ONE version-decision point — W&B semantics, revision edges, custody gaps.

Owner decision #966.3 (campaign slice S4, issue #970). Every mint seam — the S1
observer/fs_write/pack feeds and the S3 ``create_artifact`` proposal — routes its
version decision through :func:`decide_version`. There is NO version-number
arithmetic or dedup/revision logic anywhere else: :meth:`ArtifactRegistry.mint`
calls this brain UNDER its single lock (the S1 atomic-assignment property is
preserved — the decision is a pure function of the chain snapshot the lock already
holds), builds the immutable version the decision describes, and appends it.

W&B semantics (owner decision #966.3):

* **same name + same content hash → dedup** (no new version, ``created=False``);
* **same name + new content hash → v(n+1)** carrying the PROV ``wasRevisionOf``
  edge (``prior_version`` + ``prior_sha256``, the prior head);
* **kind locked at v1** — a later mint with a different kind keeps v1's kind and
  records a structured ``kind_warning`` (never a new kind, never silent);
* **re-link by hash** — a designated path re-appearing with a content hash that
  matches a KNOWN non-head version re-attaches to the identity with a
  ``custody_gap`` marker (never silently healed);
* **undesignated overwrite** detected at the next observation — content that
  differs from the recorded head with no seam having minted it mints an auto
  ``v(n+1)`` when the workspace lease is provably single-writer, else a GAP
  version (mechanism ``none``, actor unknown); the old version is NEVER mutated.

``v1`` creation emits ``artifact.created``; ``v2+`` emits ``artifact.version.added``
(carrying the revision edge) and moves the ``latest`` alias with
``artifact.alias.moved`` — both S2-allow-listed on the SSE wire. This module owns
the payload builders + guarded emitters for those two events so the version-chain
wire shape lives in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Custody,
    Mechanism,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class VersionAction(str, Enum):
    """What the single decision point decided to do with an incoming mint.

    ``DEDUP`` — the content already versions in the chain (or the on-disk head is
    unchanged): no new version, the operative version is the existing one.
    ``NEW_VERSION`` — a genuinely new immutable version (v1 ``created`` or v2+
    ``version.added``). ``RELINK`` — a custody-gap re-link by hash (content reverted
    to a known non-head state). ``GAP`` — an undesignated overwrite whose attribution
    could not be proven single-writer (mechanism ``none``, actor unknown).
    """

    DEDUP = "dedup"
    NEW_VERSION = "new_version"
    RELINK = "relink"
    GAP = "gap"


@dataclass(frozen=True)
class VersionDecision:
    """The typed outcome of :func:`decide_version` — a pure value, no I/O.

    ``version_number`` / ``kind`` / ``mechanism`` are what the appended version must
    take; ``prior_version`` + ``prior_sha256`` are the ``wasRevisionOf`` edge;
    ``kind_warning`` + ``custody_gap`` are the honest markers stamped on the version
    row. On a ``DEDUP`` decision ``deduped_onto`` is the existing operative version
    and the caller appends NOTHING.
    """

    action: VersionAction
    version_number: int = 0
    kind: ArtifactKind = ArtifactKind.OTHER
    mechanism: Mechanism = Mechanism.TOOL_SCHEMA
    prior_version: Optional[int] = None
    prior_sha256: Optional[str] = None
    kind_warning: str = ""
    custody_gap: Optional[dict[str, Any]] = None
    deduped_onto: Optional[ArtifactVersion] = None
    reason: str = ""

    @property
    def created(self) -> bool:
        """Whether a genuinely new version is to be appended (not a dedup no-op)."""
        return self.action is not VersionAction.DEDUP


def _kind_lock(record: ArtifactRecord, requested_kind: ArtifactKind) -> tuple[ArtifactKind, str]:
    """Resolve the effective kind + a warning against the kind locked at v1.

    Owner decision #966.4: the kind is immutable across a logical artifact's chain.
    A later mint requesting a different kind keeps v1's kind and gets a structured
    warning; an empty chain (v1) locks whatever kind is requested.
    """
    locked = record.locked_kind
    if locked is None or requested_kind == locked:
        return (locked or requested_kind), ""
    warning = (
        f"kind {requested_kind.value!r} ignored: locked at v1 kind {locked.value!r} "
        "(a logical artifact's kind is immutable across its version chain)"
    )
    return locked, warning


def decide_version(
    record: Optional[ArtifactRecord],
    *,
    sha256: Optional[str],
    requested_kind: ArtifactKind,
    requested_mechanism: Mechanism,
    producing: bool = True,
    lease_clean: bool = False,
) -> VersionDecision:
    """Decide dedup / new-version / re-link / gap for one incoming mint (pure).

    Called under :meth:`ArtifactRegistry.mint`'s single lock with the current chain
    snapshot. ``producing`` is ``True`` for an ordinary mint (a seam actively wrote
    the content this observation); ``False`` for a drift observation that merely
    re-observed a designated path. ``lease_clean`` gates the drift case: when the
    workspace is provably single-writer an unattributed change auto-mints a new
    version, else it becomes a GAP version. A ``None`` ``sha256`` (stat-pinned
    identity) never dedups — the content is unknown — so it always mints a new
    version, matching S1.
    """
    # v1 / empty chain — nothing to revise, the requested kind becomes the lock.
    if record is None or not record.versions:
        return VersionDecision(
            action=VersionAction.NEW_VERSION,
            version_number=1,
            kind=requested_kind,
            mechanism=requested_mechanism,
        )

    head = record.head
    assert head is not None  # non-empty chain
    effective_kind, kind_warning = _kind_lock(record, requested_kind)
    existing = record.version_for_sha(sha256) if sha256 else None

    if existing is not None:
        # The content already versions in the chain.
        if producing:
            # Ordinary same-content re-designation — W&B dedup, no new version.
            return VersionDecision(
                action=VersionAction.DEDUP,
                deduped_onto=existing,
                reason="same_sha_dedup",
            )
        # Drift observation whose on-disk content matches a KNOWN version.
        if existing.version == head.version:
            # On-disk == head: no drift at all, a clean no-op.
            return VersionDecision(
                action=VersionAction.DEDUP,
                deduped_onto=existing,
                reason="unchanged_head",
            )
        # Content reverted to a known non-head state after a gap — re-link by hash.
        # Record a NEW immutable version (never mutate the old one) carrying the
        # custody-gap marker so the re-entry is honest, not silently healed.
        return VersionDecision(
            action=VersionAction.RELINK,
            version_number=record.next_version_number(),
            kind=effective_kind,
            mechanism=requested_mechanism,
            prior_version=head.version,
            prior_sha256=head.sha256,
            kind_warning=kind_warning,
            custody_gap={
                "reason": "relink_by_hash",
                "matched_version": existing.version,
                "matched_sha256": existing.sha256,
            },
            reason="relink_by_hash",
        )

    # The content is NEW (unknown hash) or stat-pinned (unknowable identity).
    if producing:
        return VersionDecision(
            action=VersionAction.NEW_VERSION,
            version_number=record.next_version_number(),
            kind=effective_kind,
            mechanism=requested_mechanism,
            prior_version=head.version,
            prior_sha256=head.sha256,
            kind_warning=kind_warning,
            reason="revision",
        )

    # Drift observation: unknown content, NO seam minted it (undesignated overwrite).
    if lease_clean:
        # Single-writer provable — auto-mint the next version, attributed to the
        # observing seam's mechanism, with the custody gap recorded (not healed).
        return VersionDecision(
            action=VersionAction.NEW_VERSION,
            version_number=record.next_version_number(),
            kind=effective_kind,
            mechanism=requested_mechanism,
            prior_version=head.version,
            prior_sha256=head.sha256,
            kind_warning=kind_warning,
            custody_gap={
                "reason": "undesignated_overwrite",
                "lease": "clean",
                "actor": "unknown",
            },
            reason="auto_revision_lease_clean",
        )
    # Attribution unprovable — a GAP version: mechanism none, actor unknown. Precision
    # over recall (owner decision #966.10) — a gap over a falsely-attributed edge.
    return VersionDecision(
        action=VersionAction.GAP,
        version_number=record.next_version_number(),
        kind=effective_kind,
        mechanism=Mechanism.NONE,
        prior_version=head.version,
        prior_sha256=head.sha256,
        kind_warning=kind_warning,
        custody_gap={
            "reason": "undesignated_overwrite",
            "lease": "dirty",
            "actor": "unknown",
        },
        reason="gap_lease_dirty",
    )


# --------------------------------------------------------------------------- #
# Wire payloads — the version-chain + alias event shapes (owned here, S4 #970).
# --------------------------------------------------------------------------- #


def version_record_payload(
    event_id: str,
    workspace_id: str,
    name: str,
    version: ArtifactVersion,
    *,
    revision_edge: bool,
) -> dict[str, Any]:
    """Build the version record payload shared by ``created`` and ``version.added``.

    With ``revision_edge=False`` the result is byte-identical to the S1/S2
    ``artifact.created`` payload (SPEC §7.6). With ``revision_edge=True`` it adds the
    four S4 fields — ``prior_version`` / ``prior_sha256`` (the ``wasRevisionOf``
    edge) + ``kind_warning`` + ``custody_gap`` — for the ``artifact.version.added``
    event. One builder so the two events never drift.
    """
    payload: dict[str, Any] = {
        "event_id": event_id,
        "artifact_id": version.artifact_id,
        "workspace_id": workspace_id,
        "name": name,
        "version": version.version,
        "kind": version.kind.value,
        "custody": version.custody.value,
        "mechanism": version.mechanism.value,
        "sha256": version.sha256,
        "size_bytes": version.size_bytes,
        "path": version.path,
        "created_at": version.created_at,
        "annotation": version.annotation,
        "producer": dict(version.producer),
        "evidence": {
            "evidence_class": version.evidence.evidence_class.value,
            "authority": version.evidence.authority,
            "mtime": version.evidence.mtime,
        },
    }
    if revision_edge:
        payload["prior_version"] = version.prior_version
        payload["prior_sha256"] = version.prior_sha256
        payload["kind_warning"] = version.kind_warning
        payload["custody_gap"] = version.custody_gap
    return payload


def alias_moved_payload(
    event_id: str,
    workspace_id: str,
    name: str,
    *,
    alias: str,
    from_version: Optional[int],
    to_version: int,
    at: str,
) -> dict[str, Any]:
    """The ``artifact.alias.moved`` payload — a mutable pointer's move on the chain.

    ``at`` is the move timestamp; the fold applies alias moves last-writer-wins with
    ``(at, event_id)`` as the total order, so a replay of the same log — in any order
    — yields the identical alias map (fold determinism, S4 #970).
    """
    return {
        "event_id": event_id,
        "workspace_id": workspace_id,
        "name": name,
        "alias": alias,
        "from_version": from_version,
        "to_version": to_version,
        "at": at,
    }


def emit_version_added(
    app: "FastAPI",
    sid: str,
    *,
    workspace_id: str,
    name: str,
    version: ArtifactVersion,
    turn_id: str = "",
    trace_id: str = "",
) -> None:
    """Emit the durable ``artifact.version.added`` event for a v2+ revision.

    The sole emitter of the version-added event; carries the ``wasRevisionOf`` edge
    and any custody-gap / kind-warning markers. Guarded — a wire emit must never
    break a live mint.
    """
    try:
        from clio_agent.gact.artifacts.registry import ARTIFACT_VERSION_ADDED_EVENT  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415
        from clio_agent.gact.semantic_events import _event_id  # noqa: PLC0415

        event_id = _event_id()
        payload = version_record_payload(event_id, workspace_id, name, version, revision_edge=True)
        summary = f"Artifact {name} v{version.version} added"
        if version.prior_version is not None:
            summary += f" (revises v{version.prior_version})"
        _emit_semantic_event(
            app,
            sid,
            ARTIFACT_VERSION_ADDED_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            status="completed",
            summary=summary + ".",
            actor={"mechanism": version.mechanism.value},
            subject={
                "artifact_id": version.artifact_id,
                "name": name,
                "workspace_id": workspace_id,
            },
            payload=payload,
            detail_level="semantic",
        )
    except Exception:  # noqa: BLE001 — a wire emit must never break a live mint
        logger.warning(
            "artifact version.added emit skipped reason=version_added_emit_failed "
            "session=%s name=%s version=%s",
            sid,
            name,
            getattr(version, "version", "?"),
        )


def emit_alias_moved(
    app: "FastAPI",
    sid: str,
    *,
    workspace_id: str,
    name: str,
    alias: str,
    from_version: Optional[int],
    to_version: int,
    at: str,
    turn_id: str = "",
    trace_id: str = "",
) -> None:
    """Emit the durable ``artifact.alias.moved`` event for an alias pointer move.

    The sole emitter of the alias-moved event. Guarded — a wire emit must never
    break a live mint.
    """
    try:
        from clio_agent.gact.artifacts.registry import ARTIFACT_ALIAS_MOVED_EVENT  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415
        from clio_agent.gact.semantic_events import _event_id  # noqa: PLC0415

        event_id = _event_id()
        payload = alias_moved_payload(
            event_id,
            workspace_id,
            name,
            alias=alias,
            from_version=from_version,
            to_version=to_version,
            at=at,
        )
        _emit_semantic_event(
            app,
            sid,
            ARTIFACT_ALIAS_MOVED_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            status="completed",
            summary=f"Artifact {name} alias {alias!r} → v{to_version}.",
            actor={"mechanism": Mechanism.HARNESS.value},
            subject={"name": name, "workspace_id": workspace_id, "alias": alias},
            payload=payload,
            detail_level="semantic",
        )
    except Exception:  # noqa: BLE001 — a wire emit must never break a live mint
        logger.warning(
            "artifact alias.moved emit skipped reason=alias_moved_emit_failed "
            "session=%s name=%s alias=%s",
            sid,
            name,
            alias,
        )


# --------------------------------------------------------------------------- #
# Drift observation (item 4) — the honest re-observation reconcile.
# --------------------------------------------------------------------------- #


def workspace_lease_clean(app: "FastAPI", workspace_id: str) -> bool:
    """Whether the workspace has a provably single writer right now (S4 #970, item 4).

    The per-workspace-root MCP executor serializes every call under one
    ``asyncio.Lock`` (owner decision #966.10 — same-root windows are exclusive by
    construction). So while an observing seam runs INSIDE a tool call — the observer
    worker thread, its ``_OBSERVER_CALL_T0`` thread-local stamped at ``started`` — the
    lock is held and the workspace is single-writer: the lease is CLEAN. Outside any
    active call (a boot / finalize / route scan) single-writer cannot be proven, so
    the lease is DIRTY — conservative by design (precision over recall, #966.10: a GAP
    version over a falsely-attributed one). ``workspace_id`` is accepted for a future
    per-root refinement; today the proof is per active observer call.
    """
    from clio_agent.gact.artifacts.minting import _observer_call_started_at  # noqa: PLC0415

    return _observer_call_started_at() is not None


def reconcile_designated_path(
    app: "FastAPI",
    sid: str,
    *,
    name: str,
    workspace_id: str,
    path: str,
    mechanism: Mechanism,
    turn_id: str = "",
    trace_id: str = "",
    lease_clean: Optional[bool] = None,
) -> Any:
    """Re-observe a KNOWN designated path and reconcile any undesignated drift.

    The honest S4 observation (design §item 4 — no filesystem watching): given a path
    already tracked as ``(workspace_id, name)``, hash its current on-disk content and
    route the reconciliation through the single decision point with
    ``producing=False``:

    * on-disk == head → a clean no-op (the existing version, ``created=False``);
    * on-disk == a known NON-head version → a custody-gap RE-LINK by hash (a new
      immutable version with the relink marker; the identity re-attaches, the gap is
      recorded, never silently healed);
    * on-disk is UNKNOWN content that no seam minted → an auto ``v(n+1)`` when the
      workspace lease is provably single-writer (:func:`workspace_lease_clean`), else
      a GAP version (mechanism ``none``, actor unknown) — the old version is never
      mutated.

    Fires ONLY on the next designated interaction with the name; clio does not watch
    the filesystem. Returns the :class:`MintOutcome`, or ``None`` on a typed skip
    (unknown name, unresolvable/rejected containment, missing/unhashable file).
    """
    from pathlib import Path  # noqa: PLC0415

    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _contained,
        _workspace_root,
        compute_identity,
        mint_artifact_outcome,
    )
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    registry = get_registry(app)
    record = registry.get(workspace_id, name)
    if record is None or record.head is None:
        # Reconciliation is only for an established identity; a first designation
        # belongs on the ordinary mint seams.
        logger.info("artifact reconcile skipped reason=unknown_name session=%s name=%s", sid, name)
        return None
    root = _workspace_root(app, workspace_id)
    resolved = Path(path)
    if root is None:
        logger.warning("artifact reconcile skipped reason=containment_unresolved name=%s", name)
        return None
    if not _contained(resolved, root):
        logger.warning("artifact reconcile skipped reason=containment_rejected name=%s", name)
        return None
    if not resolved.is_file():
        logger.info("artifact reconcile skipped reason=designated_path_absent name=%s", name)
        return None
    try:
        evidence = compute_identity(resolved)
    except OSError:
        logger.warning("artifact reconcile skipped reason=stat_hash_failed name=%s", name)
        return None
    clean = workspace_lease_clean(app, workspace_id) if lease_clean is None else lease_clean
    return mint_artifact_outcome(
        app,
        sid,
        name=name,
        workspace_id=workspace_id,
        evidence=evidence,
        kind=record.locked_kind or ArtifactKind.OTHER,
        mechanism=mechanism,
        producer={"designation": "reconcile-observed", "session_id": sid, "turn_id": turn_id},
        custody=Custody.WORKSPACE_REFERENCED,
        path=str(resolved),
        turn_id=turn_id,
        trace_id=trace_id,
        producing=False,
        lease_clean=clean,
    )


__all__ = [
    "VersionAction",
    "VersionDecision",
    "alias_moved_payload",
    "decide_version",
    "emit_alias_moved",
    "emit_version_added",
    "reconcile_designated_path",
    "version_record_payload",
    "workspace_lease_clean",
]
