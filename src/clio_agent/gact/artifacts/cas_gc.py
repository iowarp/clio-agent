"""Reachability GC + budget enforcement for the CAS store (owner decision #966.8).

The CAS store is the only NEW artifact storage, so it carries #930-class discipline:
a byte budget and a reachability garbage collector. Reachability roots are

* **pinned aliases** — every alias target (``latest`` included) of every registered
  version keeps its content blob;
* **export manifests** — a stub root-source seam (:func:`export_manifest_shas`) S7
  closes the loop on (a session-export bundle pins the blobs it shipped);
* **artifacts used by retained sessions** — a blob a still-present session produced
  or used is REFUSED eviction with a typed ``artifact_used_by_retained_session``
  reason (relay's ``artifact_used_by_retained_job`` guard analogue).

Budget enforcement evicts UNREACHABLE blobs oldest-first until under budget, each
with a typed ``artifact.cas.evicted`` event (trace-only — the UI never renders CAS
housekeeping). Cadence: boot (:func:`run_boot_cas_gc`, alongside the #1001 boot
prune) + a cheap post-turn budget check (:func:`post_turn_cas_budget_check` — the
full reachability scan runs ONLY on a budget breach).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.cas import CASStore, cas_budget_bytes
from clio_agent.gact.artifacts.records import ArtifactRecord, Custody

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Trace-only semantic event emitted per evicted blob. Kept OFF the SSE wire via
#: ``SSE_TRACE_ONLY_EVENT_TYPES`` — CAS housekeeping is substrate, not a UI atom.
CAS_EVICTED_EVENT = "artifact.cas.evicted"

#: Trace-only semantic event emitted when the boot GC reclaims crash-orphaned ``.tmp``
#: ingest scratch (finding [2]). Trace-only substrate, like :data:`CAS_EVICTED_EVENT`.
CAS_TMP_SWEPT_EVENT = "artifact.cas.tmp_swept"

#: The typed refusal reason when an unreachable-by-alias blob is still used by a
#: retained session — relay's ``artifact_used_by_retained_job`` analogue.
USED_BY_RETAINED_REASON = "artifact_used_by_retained_session"

#: The typed skip reason when a candidate's unlink did not actually free the blob
#: (a held-open handle / AV lock) — finding [1]. No eviction event, no counted free.
CAS_EVICT_SKIPPED_REASON = "cas_evict_skipped"

#: The typed refusal reason when a version folded into the registry AFTER the GC's
#: records snapshot now references the candidate blob — finding [4] TOCTOU re-check.
RACED_MINT_REASON = "artifact_raced_concurrent_mint"

#: Grace before a ``.tmp`` ingest scratch file is treated as a crash orphan (finding
#: [2]). An in-flight ingest is sub-second; 1h is comfortably beyond any live tee.
_TMP_ORPHAN_GRACE_SECONDS = 3600.0


@dataclass
class CASGCResult:
    """Typed outcome of one workspace's budget enforcement pass.

    ``evicted`` / ``refused`` carry the per-blob decisions (each typed); a caller /
    test reads them without re-deriving from logs. ``over_budget_residual`` is set
    when the store is STILL over budget after every unreachable, evictable blob is
    gone (the residual is reachable/retained content — an honest, non-silent limit).
    """

    workspace_id: str
    total_before: int = 0
    total_after: int = 0
    budget_bytes: int = 0
    evicted: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    over_budget_residual: bool = False
    reason: str = ""
    #: Boot ``.tmp`` orphan sweep outcome (finding [2]); zero outside boot GC.
    tmp_orphans_swept: int = 0
    tmp_orphan_bytes: int = 0


# ---- in-memory running byte counter (finding [6/7]) -------------------------
# The post-turn finalize check runs ON the event loop; it must touch ZERO fs there
# (the standing liveness lesson). So the budget TRIGGER decision reads a per-workspace
# running byte total kept in memory on ``app.state`` — bumped when a new blob is
# ingested, re-synced authoritatively from disk by every off-loop scan (boot GC +
# every enforcement pass). Over-counting only schedules a harmless off-loop re-measure;
# it never under-counts genuine growth between syncs.


def _cas_byte_totals(app: "FastAPI") -> dict[str, int]:
    """The per-workspace in-memory CAS byte counter (lazily created on ``app.state``)."""
    totals = getattr(app.state, "cas_byte_totals", None)
    if not isinstance(totals, dict):
        totals = {}
        app.state.cas_byte_totals = totals
    return totals


def set_cas_bytes(app: "FastAPI", workspace_id: str, total: int) -> None:
    """Authoritatively set a workspace's running byte total (an off-loop disk re-sync)."""
    if workspace_id:
        _cas_byte_totals(app)[workspace_id] = max(0, int(total))


def get_cas_bytes(app: "FastAPI", workspace_id: str) -> Optional[int]:
    """The workspace's running byte total, or ``None`` when never synced (unknown)."""
    return _cas_byte_totals(app).get(workspace_id)


def record_cas_version(
    app: "FastAPI", workspace_id: str, custody: Custody, size_bytes: int
) -> None:
    """Bump the running byte counter when the mint funnel folds a NEW CAS version.

    Called once from the single mint funnel (:func:`minting.mint_artifact_outcome`) on
    a genuinely-created ``cas``-custody version — the one choke point every ingest seam
    passes through — so the on-loop finalize trigger sees growth without a disk walk
    (finding [6/7]). A referenced version adds nothing. Over-counting (a new version
    onto a shared, already-present blob) only schedules a harmless off-loop re-measure
    which re-syncs the counter from disk; it never under-counts real growth.
    """
    if not workspace_id or custody != Custody.CAS:
        return
    size = int(size_bytes or 0)
    if size <= 0:
        return
    totals = _cas_byte_totals(app)
    totals[workspace_id] = totals.get(workspace_id, 0) + size


def alias_reachable_shas(records: list[ArtifactRecord]) -> set[str]:
    """The content hashes kept alive by any pinned alias (``latest`` included).

    An alias maps a name to a version number; the reachable set is the sha of every
    aliased version across every record. A stat-pinned (hashless) version
    contributes nothing — it has no CAS blob to keep.
    """
    reachable: set[str] = set()
    for record in records:
        by_number = {v.version: v for v in record.versions}
        for target in record.aliases.values():
            version = by_number.get(target)
            if version is not None and version.sha256:
                reachable.add(version.sha256)
    return reachable


def export_manifest_shas(app: "FastAPI", workspace_id: str) -> set[str]:
    """Content hashes pinned by an export manifest — the S7 stub root-source seam.

    S7 (RO-Crate export) closes the loop: a shipped bundle registers the blobs it
    exported as GC roots here. Until then this reads an optional
    ``app.state.cas_export_manifest_roots`` mapping (workspace_id -> set[sha]) so the
    seam is wired and testable, and returns an empty set otherwise (never a silent
    miss — a real root source lands in S7).
    """
    roots = getattr(app.state, "cas_export_manifest_roots", None)
    if not isinstance(roots, dict):
        return set()
    entry = roots.get(workspace_id)
    return {str(s) for s in entry} if entry else set()


def retained_session_ids(app: "FastAPI", workspace_id: str) -> set[str]:
    """The ids of every session STILL retained for ``workspace_id`` (GC root source).

    A retained session is one present in the session store — its produced/used
    artifacts must not be evicted out from under it.
    """
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return set()
    try:
        sessions = store.list(workspace_id=workspace_id)
    except TypeError:
        sessions = [s for s in store.list() if getattr(s, "workspace_id", "") == workspace_id]
    except Exception:  # noqa: BLE001 — an unreadable session store yields no retained roots
        return set()
    return {str(getattr(s, "id", "") or "") for s in sessions if getattr(s, "id", "")}


def sha_used_by_retained_session(
    registry: Any,
    records: list[ArtifactRecord],
    retained_ids: set[str],
    sha256: str,
) -> Optional[str]:
    """Return a retained session id that produced/used ``sha256``, else ``None``.

    Two channels: a registered version whose ``producer.session_id`` is retained
    (the session PRODUCED it), or a TransformRecord in a retained session with a
    used/generated edge on that hash (the session USED it). Precision over recall —
    only a concrete retained link refuses eviction.
    """
    if not retained_ids:
        return None
    for record in records:
        for version in record.versions:
            if version.sha256 != sha256:
                continue
            producer_sid = str(version.producer.get("session_id") or "")
            if producer_sid in retained_ids:
                return producer_sid
    for transform in registry.all_transforms():
        sid = str(getattr(transform, "session_id", "") or "")
        if sid not in retained_ids:
            continue
        edges = list(getattr(transform, "used", [])) + list(getattr(transform, "generated", []))
        for edge in edges:
            if getattr(edge, "sha256", None) == sha256:
                return sid
    return None


def enforce_cas_budget(
    app: "FastAPI",
    workspace_id: str,
    workspace_root: str | Path,
    *,
    sid: str = "",
    budget_bytes: Optional[int] = None,
) -> CASGCResult:
    """Evict unreachable CAS blobs (oldest-first) until under budget for one workspace.

    Runs the reachability scan ONLY when the store is over budget (the cheap total
    is checked first). Roots: pinned aliases + export manifests. A candidate still
    used by a retained session is REFUSED (typed), never evicted. Every eviction is
    a typed ``artifact.cas.evicted`` trace-only event. MUST run off the event loop
    (it triggers the registry boot fold on first access).
    """
    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

    budget = cas_budget_bytes() if budget_bytes is None else budget_bytes
    store = CASStore(workspace_root)
    # finding [4] TOCTOU: snapshot the on-disk blobs BEFORE reading the registry, so a
    # version minted after this snapshot (whose blob is therefore NOT in the candidate
    # list) can never be evicted. A blob is always written before its version folds
    # (ingest → finalize → mint), so ordering blobs-first bounds the race to blobs that
    # already existed; the pre-unlink re-check below closes the remaining fold window.
    blob_snapshot = sorted(store.iter_blobs(), key=lambda b: b.mtime)
    total = store.total_bytes()
    result = CASGCResult(
        workspace_id=workspace_id,
        total_before=total,
        total_after=total,
        budget_bytes=budget,
    )
    if total <= budget:
        result.reason = "under_budget"
        set_cas_bytes(app, workspace_id, total)
        return result

    registry = get_registry(app)
    records = registry.list_for_workspace(workspace_id)
    reachable = alias_reachable_shas(records) | export_manifest_shas(app, workspace_id)
    retained = retained_session_ids(app, workspace_id)

    running = total
    for entry in blob_snapshot:
        if running <= budget:
            break
        if entry.sha256 in reachable:
            continue
        used_by = sha_used_by_retained_session(registry, records, retained, entry.sha256)
        if used_by:
            _refuse(result, workspace_id, entry, USED_BY_RETAINED_REASON, session_id=used_by)
            continue
        # finding [4]: final re-check against the FRESHEST chains immediately before the
        # unlink — a version folded after our records read may now pin this blob (its
        # ``latest`` alias) or a retained transform may now use it. Cheap, lock-held.
        if registry.is_sha_alias_reachable(
            workspace_id, entry.sha256
        ) or sha_used_by_retained_session(
            registry, registry.list_for_workspace(workspace_id), retained, entry.sha256
        ):
            _refuse(result, workspace_id, entry, RACED_MINT_REASON)
            continue
        freed = store.evict(entry.sha256)
        if freed <= 0:
            if store.has_blob(entry.sha256):
                # finding [1]: the unlink did not free the blob (held-open handle / AV
                # lock). Typed skip, NO eviction event, NO counted free — never a lie.
                _refuse(
                    result,
                    workspace_id,
                    entry,
                    CAS_EVICT_SKIPPED_REASON,
                    detail="blob_in_use",
                    path=str(entry.path),
                )
            continue
        running -= freed
        result.evicted.append({"sha256": entry.sha256, "bytes_freed": freed, "mtime": entry.mtime})
        _emit_cas_evicted(app, sid, workspace_id, entry.sha256, freed)

    # finding [1]: report the residual from a FRESH disk re-measure, never the running
    # counter (which a swallowed unlink would have drifted below reality).
    final_total = store.total_bytes()
    result.total_after = final_total
    result.over_budget_residual = final_total > budget
    set_cas_bytes(app, workspace_id, final_total)
    if result.over_budget_residual:
        logger.warning(
            "cas budget residual over budget reason=cas_budget_residual ws=%s total=%d budget=%d "
            "(residual is reachable/retained/in-use content)",
            workspace_id,
            final_total,
            budget,
        )
    return result


def _refuse(
    result: CASGCResult,
    workspace_id: str,
    entry: Any,
    reason: str,
    *,
    session_id: str = "",
    detail: str = "",
    path: str = "",
) -> None:
    """Record a typed per-blob eviction refusal/skip on ``result`` and log it."""
    row: dict[str, Any] = {
        "sha256": entry.sha256,
        "reason": reason,
        "size_bytes": entry.size_bytes,
    }
    if session_id:
        row["session_id"] = session_id
    if detail:
        row["detail"] = detail
    if path:
        row["path"] = path
    result.refused.append(row)
    logger.info(
        "cas eviction refused reason=%s ws=%s sha=%s session=%s detail=%s",
        reason,
        workspace_id,
        entry.sha256,
        session_id,
        detail,
    )


def run_boot_cas_gc(app: "FastAPI", *, budget_bytes: Optional[int] = None) -> list[CASGCResult]:
    """Enforce the CAS budget across every workspace at boot (off-loop, #1001 cadence).

    Iterates the workspace store, enforcing per-workspace. Best-effort per workspace
    (a single unreadable workspace never wedges boot). Returns the per-workspace
    results for observability/tests.
    """
    store = getattr(app.state, "workspaces", None)
    if store is None:
        return []
    results: list[CASGCResult] = []
    try:
        workspaces = store.list()
    except Exception:  # noqa: BLE001 — an unreadable workspace store is a no-op boot GC
        logger.warning("cas boot gc skipped reason=workspace_store_unreadable")
        return []
    for ws in workspaces:
        wid = str(getattr(ws, "id", "") or "")
        root = str(getattr(ws, "root_path", "") or "")
        if not wid or not root:
            continue
        try:
            swept, swept_bytes = _sweep_tmp_orphans(app, wid, root)
            result = enforce_cas_budget(app, wid, root, budget_bytes=budget_bytes)
            result.tmp_orphans_swept = swept
            result.tmp_orphan_bytes = swept_bytes
            results.append(result)
        except Exception:  # noqa: BLE001 — a per-workspace GC failure never wedges boot
            logger.warning("cas boot gc skipped reason=cas_gc_failed ws=%s", wid)
    return results


def _sweep_tmp_orphans(
    app: "FastAPI", workspace_id: str, workspace_root: str | Path, *, sid: str = ""
) -> tuple[int, int]:
    """Reclaim crash-orphaned ``.tmp`` ingest scratch at boot (finding [2]).

    Deletes ``.tmp`` files older than :data:`_TMP_ORPHAN_GRACE_SECONDS` (an in-flight
    ingest is sub-second, so anything older is a crash/kill orphan) and, when any were
    reclaimed, emits a typed ``cas_tmp_orphans_swept`` signal (never a silent reclaim).
    Returns ``(count, bytes)``. Runs BEFORE the budget scan so the sweep's headroom
    counts toward the same pass.
    """
    swept, swept_bytes = CASStore(workspace_root).sweep_tmp_orphans(
        grace_seconds=_TMP_ORPHAN_GRACE_SECONDS
    )
    if swept:
        logger.info(
            "cas tmp orphans swept reason=cas_tmp_orphans_swept ws=%s count=%d bytes=%d",
            workspace_id,
            swept,
            swept_bytes,
        )
        _emit_tmp_orphans_swept(app, sid, workspace_id, swept, swept_bytes)
    return (swept, swept_bytes)


def post_turn_cas_budget_check(
    app: "FastAPI", workspace_id: str, *, sid: str = "", budget_bytes: Optional[int] = None
) -> Optional[CASGCResult]:
    """Cheap post-turn budget check: enforce only when the store is over budget.

    Reads the store total (a bounded ``du`` over the budget-capped CAS dir) and, only
    on a breach, runs the full reachability enforcement. Returns the GC result when a
    scan ran, else ``None`` (under budget — the common, cheap path). MUST run off the
    event loop.
    """
    root = _workspace_root(app, workspace_id)
    if root is None:
        return None
    budget = cas_budget_bytes() if budget_bytes is None else budget_bytes
    total = CASStore(root).total_bytes()
    if total <= budget:
        # Re-sync the in-memory counter from disk (finding [6/7]) — this off-loop path
        # is the authoritative correction for any on-loop over-count.
        set_cas_bytes(app, workspace_id, total)
        return None
    return enforce_cas_budget(app, workspace_id, root, sid=sid, budget_bytes=budget)


def finalize_cas_budget_check(app: "FastAPI", session: Any, sid: str) -> None:
    """Zero-fs post-turn CAS budget TRIGGER for ``turn_finalize`` (one-line caller).

    Owns the whole finalize seam (no-accretion — ``turn_finalize`` stays a one-line
    caller). ``turn_finalize`` runs ON the event loop, so this MUST NOT touch the
    filesystem here (the standing liveness lesson, finding [6/7]): it reads only the
    in-memory running byte counter and, when that counter breaches the budget (or is
    unknown), schedules the full scan + eviction on a worker thread — never inline.
    Under budget it is a single dict lookup. Any failure is swallowed with a typed
    reason; a housekeeping GC pass must never break a turn's answer.
    """
    try:
        workspace_id = str(getattr(session, "workspace_id", "") or "")
        if not workspace_id:
            return
        budget = cas_budget_bytes()
        total = get_cas_bytes(app, workspace_id)
        if total is not None and total <= budget:
            return  # under budget per the running counter — zero fs, no scan
        # Unknown (never synced) OR over budget → do the fs walk + GC OFF the loop.
        _schedule_offloop_budget_check(app, workspace_id, sid)
    except Exception:  # noqa: BLE001 — a CAS budget pass must never break a turn
        logger.warning(
            "cas post-turn budget check skipped reason=cas_post_turn_failed session=%s", sid
        )


def _schedule_offloop_budget_check(app: "FastAPI", workspace_id: str, sid: str) -> None:
    """Run the blocking budget scan off the event loop (finding [6/7]).

    On the running loop this fire-and-forgets the scan into the default executor so no
    filesystem I/O touches the loop thread. With no running loop (a sync/test context)
    it runs inline — already off-loop by definition.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _offloop_budget_worker(app, workspace_id, sid)
        return
    loop.run_in_executor(None, _offloop_budget_worker, app, workspace_id, sid)


def _offloop_budget_worker(app: "FastAPI", workspace_id: str, sid: str) -> None:
    """Worker-thread body: the real (blocking) post-turn budget check + typed guard."""
    try:
        post_turn_cas_budget_check(app, workspace_id, sid=sid)
    except Exception:  # noqa: BLE001 — a CAS budget pass must never break a turn
        logger.warning(
            "cas post-turn budget check skipped reason=cas_post_turn_failed session=%s", sid
        )


def _workspace_root(app: "FastAPI", workspace_id: str) -> Optional[str]:
    """Resolve a workspace's root path, or ``None`` when unresolvable."""
    store = getattr(app.state, "workspaces", None)
    if store is None or not workspace_id:
        return None
    try:
        ws = store.get(workspace_id)
    except Exception:  # noqa: BLE001 — an unresolvable workspace is a skip
        return None
    root = str(getattr(ws, "root_path", "") or "") if ws is not None else ""
    return root or None


def _emit_cas_evicted(
    app: "FastAPI", sid: str, workspace_id: str, sha256: str, bytes_freed: int
) -> None:
    """Best-effort trace-only ``artifact.cas.evicted`` emit (never breaks GC)."""
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            CAS_EVICTED_EVENT,
            status="completed",
            summary=f"Evicted unreachable CAS blob {sha256[:12]} ({bytes_freed} bytes).",
            actor={"mechanism": "harness"},
            subject={"workspace_id": workspace_id, "sha256": sha256},
            payload={
                "workspace_id": workspace_id,
                "sha256": sha256,
                "bytes_freed": bytes_freed,
                "reason": "cas_budget_evicted",
            },
        )
    except Exception:  # noqa: BLE001 — a housekeeping emit must never break GC
        logger.warning(
            "cas eviction event emit skipped reason=cas_evicted_emit_failed ws=%s sha=%s",
            workspace_id,
            sha256,
        )


def _emit_tmp_orphans_swept(
    app: "FastAPI", sid: str, workspace_id: str, count: int, bytes_freed: int
) -> None:
    """Best-effort trace-only ``artifact.cas.tmp_swept`` emit (never breaks boot GC)."""
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            sid,
            CAS_TMP_SWEPT_EVENT,
            status="completed",
            summary=f"Swept {count} crash-orphaned CAS temp file(s) ({bytes_freed} bytes).",
            actor={"mechanism": "harness"},
            subject={"workspace_id": workspace_id},
            payload={
                "workspace_id": workspace_id,
                "count": count,
                "bytes": bytes_freed,
                "reason": "cas_tmp_orphans_swept",
            },
        )
    except Exception:  # noqa: BLE001 — a housekeeping emit must never break boot GC
        logger.warning(
            "cas tmp swept event emit skipped reason=cas_tmp_swept_emit_failed ws=%s", workspace_id
        )


__all__ = [
    "CAS_EVICTED_EVENT",
    "CAS_EVICT_SKIPPED_REASON",
    "CAS_TMP_SWEPT_EVENT",
    "RACED_MINT_REASON",
    "USED_BY_RETAINED_REASON",
    "CASGCResult",
    "alias_reachable_shas",
    "enforce_cas_budget",
    "export_manifest_shas",
    "finalize_cas_budget_check",
    "get_cas_bytes",
    "post_turn_cas_budget_check",
    "record_cas_version",
    "retained_session_ids",
    "run_boot_cas_gc",
    "set_cas_bytes",
    "sha_used_by_retained_session",
]
