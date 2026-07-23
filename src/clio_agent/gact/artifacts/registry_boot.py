"""Boot fold — rebuild the artifact registry projection from the durable log.

Split out of :mod:`clio_agent.gact.artifacts.registry` (no-accretion): the registry
file owns the in-memory projection + fold + mint + queries; this module owns the
one-time BOOT rebuild that reconstructs that projection from the event log (ARC
``_events`` UNION the durable JSONL trace).

UNION-folds BOTH sources (owner decision #966.8): the fold is idempotent
(``event_id`` dedup + same-sha no-op + keep-first), so folding both is safe and
recovers deleted-session history. Only when NEITHER source is reachable does the
registry boot empty with a typed ``capture_released`` reason (a reachable-but-empty
source is a clean empty registry, not a degrade).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from clio_agent.arc.clio_core_liveness import ClioCoreRuntimeLostError
from clio_agent.gact.artifacts.registry import _FOLD_EVENT_TYPES, ArtifactRegistry

if TYPE_CHECKING:
    import asyncio

    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: The ARC segment scope the boot fold scans (the ``_events`` log). Named in the typed
#: stall error so an operator knows exactly which ARC scope wedged.
_ARC_FOLD_SCOPE = "_events"


class ArtifactRegistryBootStalled(RuntimeError):
    """The ARC ``_events`` boot fold wedged on a hung native clio-core RPC (#971).

    A stalled ``GetBlob`` (the C-extension spins holding the GIL — see upstream
    iowarp/clio-core#793) cannot be recovered in-process: the per-RPC liveness
    ladder ABANDONS the hung worker, but the abandoned native call keeps the GIL, so
    abandonment does not free the process. Rather than let that freeze a mid-turn tool
    completion (the lazy first-access fold path), the BOOT fold raises this typed,
    actionable error so the failure is LOUD and points at the wedged store: the caller
    (agent construction) leaves the agent unready with the store path in the message,
    so ``POST /messages`` 503s with an operator-actionable reason (rotate the ARC store
    aside, or run ``clio doctor``) instead of the whole server going dark.

    ``store_path`` is the on-disk ARC store directory to rotate; ``scope`` is the ARC
    segment scope that wedged; ``reason`` is the machine tag.
    """

    def __init__(
        self,
        message: str,
        *,
        store_path: str,
        scope: str = _ARC_FOLD_SCOPE,
        reason: str = "arc_boot_fold_stalled",
    ) -> None:
        super().__init__(message)
        self.store_path = store_path
        self.scope = scope
        self.reason = reason


def _arc_store_path(arc: object) -> str:
    """Best-effort on-disk path of an ARC store, for the actionable stall message.

    Reads ``arc.data_dir`` (the :class:`~clio_agent.arc.memory.ARCMemory` store root);
    returns ``"<unknown>"`` when the ARC exposes no path (a fake in a test).
    """
    data_dir = getattr(arc, "data_dir", None)
    return str(data_dir) if data_dir else "<unknown>"


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

    # LOUD pre-fold marker (#971 defect 1b): emitted BEFORE the first native RPC so that,
    # even if a clio-core GetBlob wedges holding the GIL and freezes the process, the LAST
    # log line points squarely at the artifact-registry fold + the store to rotate.
    from clio_agent.gact.runtime.globals import _PROCESS_ARC  # noqa: PLC0415

    _arc = getattr(app.state, "arc", None) or _PROCESS_ARC
    logger.info(
        "artifact registry boot fold starting store=%s scope=%s",
        _arc_store_path(_arc),
        _ARC_FOLD_SCOPE,
    )

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
    except ClioCoreRuntimeLostError as exc:
        # #971 defect 1b: an EXHAUSTED per-RPC liveness ladder (a hung native GetBlob) is
        # NOT a "configured-but-unreadable" degrade — it is a wedged store. Do NOT silently
        # boot an empty registry: re-raise a typed, actionable stall naming the store to
        # rotate so agent construction aborts LOUD (the boot-time placement converts a
        # mid-turn whole-server freeze into a diagnosable boot failure).
        store_path = _arc_store_path(arc)
        logger.error(
            "artifact registry boot fold STALLED reason=arc_boot_fold_stalled store=%s "
            "scope=%s cause=%r",
            store_path,
            _ARC_FOLD_SCOPE,
            exc,
        )
        raise ArtifactRegistryBootStalled(
            f"artifact registry boot fold stalled reading ARC scope {_ARC_FOLD_SCOPE!r} "
            f"from store {store_path}: a native clio-core RPC hung and the liveness "
            "ladder could not recover it. Rotate the ARC store aside (rename it) or run "
            "`clio doctor`, then restart.",
            store_path=store_path,
        ) from exc
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


async def boot_fold_artifact_registry_offloop(
    app: "FastAPI", loop: "asyncio.AbstractEventLoop"
) -> bool:
    """Fold the artifact registry projection ONCE at boot, OFF the event loop (#971).

    The gact lifespan calls this from agent construction, before the agent is announced
    ready. The registry is a projection over the ARC ``_events`` log; rebuilding it is an
    O(corpus) native-RPC-heavy scan. Folding here — off-loop, once — means the
    tool-completion hot path (``observe_tool_transform -> get_registry``) always finds a
    PRE-BUILT projection and never pays the fold (defect 2); ``get_registry``'s lazy
    first-access rebuild remains only as the in-process/test fallback for apps built
    without this lifespan.

    Returns ``True`` when the fold completed (registry stamped on ``app.state``) and the
    agent may be announced ready. Returns ``False`` when the ARC store WEDGED on a hung
    native RPC (defect 1b): the store path is stamped into ``app.state.agent_init_error``
    and logged LOUD + actionable so ``POST /messages`` 503s with an operator-actionable
    reason (rotate the ARC store aside / run ``clio doctor``) instead of the whole server
    freezing mid-turn. The boot-time placement converts an unrecoverable in-process GIL
    freeze into a diagnosable boot failure.
    """
    # #978 (B4): wire the network chokepoint's egress recorder to THIS app now that ARC is
    # live — every confined child's ``net.egress`` then lands on the durable trace + ARC and
    # feeds the ``used web:domain@time`` ingest edge that enriches this very registry. Rides
    # the artifact-provenance boot (no new god-file call site); the owner module owns the logic.
    from clio_agent.gact.artifacts.ingest_edges import install_egress_recorder  # noqa: PLC0415

    install_egress_recorder(app)
    try:
        await loop.run_in_executor(None, rebuild_registry_at_boot, app)
    except ArtifactRegistryBootStalled as exc:
        app.state.agent_init_error = str(exc)
        logger.error(
            "artifact registry boot fold stalled reason=%s store=%s scope=%s; the agent "
            "will NOT come ready. Rotate the ARC store aside or run `clio doctor`, then "
            "restart.",
            exc.reason,
            exc.store_path,
            exc.scope,
        )
        print(
            f"[clio-agent-gact] artifact registry boot fold STALLED on {exc.store_path} "
            "(a clio-core GetBlob hung); POST /messages will keep returning 503. "
            "Rotate the ARC store aside or run `clio doctor`.",
            flush=True,
        )
        return False
    return True


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
    "ArtifactRegistryBootStalled",
    "boot_fold_artifact_registry_offloop",
    "rebuild_registry_at_boot",
]
