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

from clio_agent.gact.artifacts.registry import _FOLD_EVENT_TYPES, ArtifactRegistry

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


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


__all__ = ["rebuild_registry_at_boot"]
