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

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.cas import CASStore, cas_budget_bytes
from clio_agent.gact.artifacts.records import ArtifactRecord

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Trace-only semantic event emitted per evicted blob. Kept OFF the SSE wire via
#: ``SSE_TRACE_ONLY_EVENT_TYPES`` — CAS housekeeping is substrate, not a UI atom.
CAS_EVICTED_EVENT = "artifact.cas.evicted"

#: The typed refusal reason when an unreachable-by-alias blob is still used by a
#: retained session — relay's ``artifact_used_by_retained_job`` analogue.
USED_BY_RETAINED_REASON = "artifact_used_by_retained_session"


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
    total = store.total_bytes()
    result = CASGCResult(
        workspace_id=workspace_id,
        total_before=total,
        total_after=total,
        budget_bytes=budget,
    )
    if total <= budget:
        result.reason = "under_budget"
        return result

    registry = get_registry(app)
    records = registry.list_for_workspace(workspace_id)
    reachable = alias_reachable_shas(records) | export_manifest_shas(app, workspace_id)
    retained = retained_session_ids(app, workspace_id)

    running = total
    for entry in sorted(store.iter_blobs(), key=lambda b: b.mtime):
        if running <= budget:
            break
        if entry.sha256 in reachable:
            continue
        used_by = sha_used_by_retained_session(registry, records, retained, entry.sha256)
        if used_by:
            result.refused.append(
                {
                    "sha256": entry.sha256,
                    "reason": USED_BY_RETAINED_REASON,
                    "session_id": used_by,
                    "size_bytes": entry.size_bytes,
                }
            )
            logger.info(
                "cas eviction refused reason=%s ws=%s sha=%s session=%s",
                USED_BY_RETAINED_REASON,
                workspace_id,
                entry.sha256,
                used_by,
            )
            continue
        freed = store.evict(entry.sha256)
        running -= freed
        result.evicted.append({"sha256": entry.sha256, "bytes_freed": freed, "mtime": entry.mtime})
        _emit_cas_evicted(app, sid, workspace_id, entry.sha256, freed)

    result.total_after = running
    result.over_budget_residual = running > budget
    if result.over_budget_residual:
        logger.warning(
            "cas budget residual over budget reason=cas_budget_residual ws=%s total=%d budget=%d "
            "(residual is reachable/retained content)",
            workspace_id,
            running,
            budget,
        )
    return result


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
            results.append(enforce_cas_budget(app, wid, root, budget_bytes=budget_bytes))
        except Exception:  # noqa: BLE001 — a per-workspace GC failure never wedges boot
            logger.warning("cas boot gc skipped reason=cas_gc_failed ws=%s", wid)
    return results


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
    if CASStore(root).total_bytes() <= budget:
        return None
    return enforce_cas_budget(app, workspace_id, root, sid=sid, budget_bytes=budget)


def finalize_cas_budget_check(app: "FastAPI", session: Any, sid: str) -> None:
    """Guarded post-turn CAS budget check for ``turn_finalize`` (one-line caller).

    Owns the whole finalize seam (no-accretion — ``turn_finalize`` stays a one-line
    caller): resolves the session's workspace id and runs the cheap
    :func:`post_turn_cas_budget_check`, swallowing any failure with a typed reason. A
    housekeeping GC pass must never break a turn's answer. Finalize runs off the event
    loop (its mints already fold the registry), so the blocking check is safe here.
    """
    try:
        workspace_id = str(getattr(session, "workspace_id", "") or "")
        if workspace_id:
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


__all__ = [
    "CAS_EVICTED_EVENT",
    "USED_BY_RETAINED_REASON",
    "CASGCResult",
    "alias_reachable_shas",
    "enforce_cas_budget",
    "export_manifest_shas",
    "finalize_cas_budget_check",
    "post_turn_cas_budget_check",
    "retained_session_ids",
    "run_boot_cas_gc",
    "sha_used_by_retained_session",
]
