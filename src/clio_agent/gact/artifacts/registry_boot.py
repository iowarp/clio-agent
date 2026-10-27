"""Boot — open the artifact registry from clio-core records, or migrate once.

Split out of :mod:`clio_agent.gact.artifacts.registry` (no-accretion). The agent
process is stateless: clio-core is the memory. When the ``artifact_index`` migration
marker exists, boot reads that ONE record and serves the registry from point gets
(:mod:`~clio_agent.gact.artifacts.registry_store`) — it folds nothing. A store that
predates the records (no marker) is migrated ONCE: the full fold below (ARC
``_events`` UNION the durable JSONL trace, owner decision #966.8 — idempotent by
``event_id`` dedup + same-sha no-op + keep-first, so folding both is safe and
recovers deleted-session history) feeds
:func:`~clio_agent.gact.artifacts.registry_migration.migrate`, which writes the
records and the marker last. Only a process with NO clio-core record store keeps
the full fold as its in-process registry (typed ``artifact_index_unavailable``).
Only when NEITHER fold source is reachable does the registry boot empty with a
typed ``capture_released`` reason (a reachable-but-empty source is a clean empty
registry, not a degrade).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from clio_agent.arc.clio_core_liveness import ClioCoreRuntimeLostError
from clio_agent.gact.artifacts import registry_migration, registry_store
from clio_agent.gact.artifacts.registry import _FOLD_EVENT_TYPES, ArtifactRegistry

if TYPE_CHECKING:
    import asyncio

    from fastapi import FastAPI

    from clio_agent.arc.store_protocol import ARCStore

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
    #: Sessions whose ``_events`` could not be read (skipped, typed) — recorded on
    #: the migration marker so the gap stays visible after boot stops folding.
    skipped_sessions: tuple[str, ...] = field(default_factory=tuple)


def rebuild_registry_at_boot(app: "FastAPI") -> ArtifactRegistry:
    """Open ``app.state.artifact_registry`` at boot (or first access).

    Migrated store (marker present): a registry over the clio-core records — ONE
    marker read, no fold. Otherwise the full UNION fold (owner decision #966.8,
    finding [2]) runs once and, with a record store, is written as records (marker
    last) by :func:`~clio_agent.gact.artifacts.registry_migration.migrate`. A failed
    migration or no record store keeps the folded projection in process, typed. Only
    when NEITHER source is reachable does the registry boot empty with a typed
    ``capture_released`` reason (finding [11]); no marker is written then, so the
    lazy retry migrates. ``app.state`` is assigned only once the registry is
    complete, so a concurrent reader never sees a half-built projection.
    """
    _arc = _boot_arc(app)
    store = registry_store.index_store(app)
    if store is not None:
        try:
            marker = registry_migration.read_marker(store)
        except ClioCoreRuntimeLostError as exc:
            raise _raise_stall(_arc, exc, scope=registry_store.ARTIFACT_INDEX_KIND) from exc
        except registry_store.STORE_ERRORS as exc:
            logger.warning(
                "artifact index marker unreadable reason=artifact_index_unreadable cause=%r; "
                "serving the in-process fold this boot",
                exc,
            )
            store, marker = None, None
        if marker is not None and store is not None:
            registry = ArtifactRegistry(registry_store.store_backend(app, store))
            logger.info(
                "artifact registry served from clio-core records reason=artifact_index_ready "
                "store=%s migrated_at=%s (no boot fold)",
                _arc_store_path(_arc),
                marker.get("migrated_at"),
            )
            app.state.artifact_registry = registry
            return registry
    if store is not None:
        # The one-time migration fold: checked and run through the store-migration owner,
        # so an owed fold at boot is typed (``migration_owed_at_boot``) and reported.
        from clio_agent.gact import store_migrations  # noqa: PLC0415 - import cycle

        if store_migrations.ensure_at_boot(app, "artifact_index"):
            return cast(ArtifactRegistry, app.state.artifact_registry)
        return rebuild_registry_at_boot(app)  # migrated meanwhile: open the records
    return _fold_registry(app, None)


def artifact_index_owed(app: "FastAPI") -> bool:
    """Whether this store has a record store but no artifact-index marker yet.

    A store with no record store has nothing to migrate (the registry is the in-process
    fold, typed ``artifact_index_unavailable``). An unreadable marker is not decided
    here: :func:`rebuild_registry_at_boot` re-reads it and types the failure.
    """
    store = registry_store.index_store(app)
    if store is None:
        return False
    try:
        return registry_migration.read_marker(store) is None
    except (ClioCoreRuntimeLostError, *registry_store.STORE_ERRORS):
        return False


def migrate_artifact_index(app: "FastAPI") -> dict[str, int]:
    """The store migration ``artifact_index``: fold once and write the records + marker."""
    registry = _fold_registry(app, registry_store.index_store(app))
    return {"records": registry.count(), "fold_conflicts": len(registry.fold_conflicts)}


def _boot_arc(app: "FastAPI") -> object:
    from clio_agent.gact.runtime.globals import _PROCESS_ARC  # noqa: PLC0415

    return getattr(app.state, "arc", None) or _PROCESS_ARC


def _fold_registry(app: "FastAPI", store: "ARCStore | None") -> ArtifactRegistry:
    """The full UNION fold; with a record store, written as records (marker last)."""
    _arc = _boot_arc(app)
    # LOUD pre-fold marker (#971 defect 1b): emitted BEFORE the first native RPC so that,
    # even if a clio-core GetBlob wedges holding the GIL and freezes the process, the LAST
    # log line points squarely at the artifact-registry fold + the store to rotate.
    logger.info(
        "artifact registry boot fold starting store=%s scope=%s migrate=%s",
        _arc_store_path(_arc),
        _ARC_FOLD_SCOPE,
        store is not None,
    )
    folded = ArtifactRegistry()
    arc_fold = _fold_from_arc(app, folded)
    jsonl_fold = _fold_from_jsonl(app, folded)

    if not arc_fold.reachable and not jsonl_fold.reachable:
        folded.capture_released = {
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
            folded.count(),
            len(folded.fold_conflicts),
        )
    registry = folded
    if store is not None and folded.capture_released is None:
        try:
            registry_migration.migrate(store, folded, skipped_sessions=arc_fold.skipped_sessions)
        except ClioCoreRuntimeLostError as exc:
            raise _raise_stall(_arc, exc, scope=registry_store.ARTIFACT_INDEX_KIND) from exc
        except registry_store.STORE_ERRORS as exc:
            logger.warning(
                "artifact index migration failed reason=artifact_index_migration_failed "
                "cause=%r; serving the in-process fold this boot, the next boot retries",
                exc,
            )
        else:
            registry = ArtifactRegistry(registry_store.store_backend(app, store))
            registry.fold_conflicts = list(folded.fold_conflicts)
    app.state.artifact_registry = registry
    return registry


def _raise_stall(
    arc: object, exc: Exception, *, scope: str = _ARC_FOLD_SCOPE
) -> "ArtifactRegistryBootStalled":
    """Convert a hung-native-RPC liveness loss into the typed, actionable stall."""
    store_path = _arc_store_path(arc)
    logger.error(
        "artifact registry boot fold STALLED reason=arc_boot_fold_stalled store=%s "
        "scope=%s cause=%r",
        store_path,
        scope,
        exc,
    )
    raise ArtifactRegistryBootStalled(
        f"artifact registry boot fold stalled reading ARC scope {scope!r} "
        f"from store {store_path}: a native clio-core RPC hung and the liveness "
        "ladder could not recover it. Rotate the ARC store aside (rename it) or run "
        "`clio doctor`, then restart.",
        store_path=store_path,
        scope=scope,
    ) from exc


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

    def _fold_content(content: object) -> None:
        nonlocal folded_any
        if not isinstance(content, dict):
            return
        event_type = str(content.get("event_type") or "")
        if event_type not in _FOLD_EVENT_TYPES:
            return
        payload = content.get("payload")
        if isinstance(payload, dict):
            registry.fold_event_by_type(event_type, payload)
            folded_any = True

    # Per-session granularity when the observer exposes it (the real ARC live
    # plane): ONE session whose segments fail to read (observed live: a soured
    # CTE GetBlob after long daemon churn) skips with a typed reason instead of
    # abandoning the whole source — a partial fold beats an empty registry.
    session_ids_fn = getattr(observer, "_event_session_ids", None)
    per_session = getattr(observer, "iter_session_event_segments", None)
    if callable(session_ids_fn) and callable(per_session):
        read_session_ids = cast(Callable[[], Iterable[str]], session_ids_fn)
        read_session_segments = cast(Callable[[str], Iterable[object]], per_session)
        try:
            session_ids = list(read_session_ids())
        except ClioCoreRuntimeLostError as exc:
            raise _raise_stall(arc, exc) from exc
        except Exception as exc:  # noqa: BLE001 — enumeration itself unreadable
            logger.warning(
                "artifact boot fold ARC source unreadable reason=arc_iter_failed "
                "folded_any=%s cause=%r",
                folded_any,
                exc,
            )
            return _SourceFold(reachable=False, folded_any=folded_any)
        skipped: list[str] = []
        for session_id in session_ids:
            try:
                for segment in read_session_segments(session_id):
                    _fold_content(getattr(segment, "content", None))
            except ClioCoreRuntimeLostError as exc:
                raise _raise_stall(arc, exc) from exc
            except Exception as exc:  # noqa: BLE001 — skip ONE session, keep folding
                skipped.append(session_id)
                logger.warning(
                    "artifact boot fold skipped a session reason=segment_unreadable "
                    "session=%s cause=%r",
                    session_id,
                    exc,
                )
                continue
        if skipped:
            logger.warning(
                "artifact boot fold completed with skips reason=segment_unreadable "
                "skipped_sessions=%d folded_any=%s",
                len(skipped),
                folded_any,
            )
        return _SourceFold(reachable=True, folded_any=folded_any, skipped_sessions=tuple(skipped))

    try:
        for content in reader():
            _fold_content(content)
    except ClioCoreRuntimeLostError as exc:
        # #971 defect 1b: an EXHAUSTED per-RPC liveness ladder (a hung native GetBlob)
        # is a wedged store, never a silent empty boot — typed + actionable.
        raise _raise_stall(arc, exc) from exc
    except Exception as exc:  # noqa: BLE001 — a configured-but-unreadable source is unreachable
        logger.warning(
            "artifact boot fold ARC source unreadable reason=arc_iter_failed folded_any=%s "
            "cause=%r",
            folded_any,
            exc,
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
    roots = tuple(root for root in _trace_dirs(app) if root.exists())
    if not roots:
        return _SourceFold(reachable=False, folded_any=False)
    import json  # noqa: PLC0415

    folded_any = False
    paths = sorted({path for root in roots for path in root.glob("*.semantic.jsonl")})
    for path in paths:
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
    """Open the artifact registry at boot, OFF the event loop (#971).

    The gact lifespan calls this from agent construction, before the agent is announced
    ready. A migrated store costs one marker read here (no fold); an unmigrated one pays
    the one-time O(corpus) migration fold. Opening here — off-loop — means the
    tool-completion hot path (``observe_tool_transform -> get_registry``) always finds an
    installed registry (defect 2); ``get_registry``'s lazy first-access open remains only
    as the in-process/test fallback for apps built without this lifespan.

    Returns ``True`` when the registry is installed (registry stamped on ``app.state``) and the
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
    # B5 (#979): wire the deny-mode egress GATE (opt-in per workspace — inert until a workspace
    # opts in) and replay each workspace's persisted root grants into the live territory registry
    # so a recorded grant survives a restart (RULE 4: no new store — it rides the workspace record).
    from clio_agent.gact.runtime.grants import (  # noqa: PLC0415
        install_egress_gate,
        replay_persisted_root_grants,
    )

    install_egress_gate(app)
    replay_persisted_root_grants(app)
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


def _trace_dirs(app: "FastAPI") -> tuple[Path, ...]:
    """Resolve every durable-trace directory advertised by the trace backend."""
    backend = getattr(app.state, "semantic_trace_backend", None)
    advertised = getattr(backend, "replay_paths", None)
    raw_paths = advertised if advertised is not None else (getattr(backend, "path", None),)
    roots: list[Path] = []
    for raw_path in raw_paths:
        if not raw_path:
            continue
        candidate = raw_path if isinstance(raw_path, Path) else Path(str(raw_path))
        root = candidate if candidate.is_dir() or not candidate.suffix else candidate.parent
        if root not in roots:
            roots.append(root)
    return tuple(roots)


__all__ = [
    "ArtifactRegistryBootStalled",
    "boot_fold_artifact_registry_offloop",
    "rebuild_registry_at_boot",
]
