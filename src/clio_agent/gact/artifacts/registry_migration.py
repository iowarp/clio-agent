"""One-time migration: write ``artifact_index`` records from the full event-log fold.

Owner module (no accretion). A clio-core store that predates the index records has
no marker; boot then folds the event log ONCE (ARC ``_events`` UNION the durable
JSONL trace — :mod:`~clio_agent.gact.artifacts.registry_boot`) into an in-memory
projection and this module writes every record that projection holds, the marker
LAST. A crash mid-migration leaves no marker, so the next boot redoes it (every put
is an idempotent overwrite). Once the marker exists, boot reads it and folds
nothing.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.artifacts.registry_store import (
    ARTIFACT_INDEX_KIND,
    MARKER_KEY,
    WORKSPACES_KEY,
    encode,
    index_key,
    put_bodies,
    record_body,
)

if TYPE_CHECKING:
    from clio_agent.arc.store_protocol import ARCStore
    from clio_agent.gact.artifacts.registry import ArtifactRegistry

logger = logging.getLogger(__name__)

#: Bumped when the record layout changes incompatibly (a new layout re-migrates).
INDEX_LAYOUT_VERSION = 1


def read_marker(store: "ARCStore") -> Optional[dict[str, Any]]:
    """The migration marker, or ``None`` when this store has not been migrated.

    A marker from another layout version reads as absent (re-migrate). Store errors
    propagate: the caller turns them into its typed stall/unreadable reasons.
    """
    raw = store.get(ARTIFACT_INDEX_KIND, MARKER_KEY)
    if raw is None:
        return None
    marker = json.loads(raw)
    if not isinstance(marker, dict) or marker.get("layout") != INDEX_LAYOUT_VERSION:
        return None
    return marker


def index_bodies(folded: "ArtifactRegistry") -> dict[str, Any]:
    """Every ``artifact_index`` record body the folded projection implies."""
    backend = folded._backend  # noqa: SLF001 — the fold's MemoryBackend, owner pairing
    bodies: dict[str, Any] = {}
    lists: dict[str, list[str]] = {}

    def append(key: str, item: str) -> None:
        items = lists.setdefault(key, [])
        if item not in items:
            items.append(item)

    alias_keys = backend.all_alias_keys()
    for record in backend.all_records():
        ws, name = record.workspace_id, record.name
        append(WORKSPACES_KEY, ws)
        append(index_key("w", ws), name)
        keys = alias_keys.get((ws, name), {})
        bodies[index_key("r", ws, name)] = record_body(record, keys, {})
        for version in record.versions:
            bodies[index_key("a", version.artifact_id)] = [ws, name]
            producer_sid = str(version.producer.get("session_id") or "")
            if producer_sid:
                append(index_key("p", producer_sid), version.artifact_id)
    for transform in backend.all_transforms():
        append(index_key("s", transform.session_id), transform.call_id)
        for edge in transform.generated:
            if edge.artifact_id:
                append(index_key("g", edge.artifact_id), transform.call_id)
        for edge in transform.used:
            if edge.artifact_id:
                append(index_key("u", edge.artifact_id), transform.call_id)
        bodies[index_key("t", transform.call_id)] = {
            "transform": transform.model_dump(mode="json"),
            "pending": {},
        }
    for session_id, ids in backend.all_used().items():
        bodies[index_key("x", session_id)] = {"ids": ids, "pending": {}}
    for artifact_id, annotation in backend.all_enrichment().items():
        bodies[index_key("e", artifact_id)] = {"annotation": annotation, "pending": {}}
    bodies.update(lists)
    return bodies


def migrate(
    store: "ARCStore",
    folded: "ArtifactRegistry",
    *,
    skipped_sessions: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Write the folded projection as index records, then the marker (last).

    ``skipped_sessions`` (sessions whose ``_events`` could not be read during the
    fold) are recorded on the marker and logged typed: the marker still lands so
    boot stops refolding, and an operator can see what the index never saw.
    """
    bodies = index_bodies(folded)
    put_bodies(store, bodies)
    marker = {
        "layout": INDEX_LAYOUT_VERSION,
        "migrated_at": time.time(),
        "records": folded.count(),
        "bodies": len(bodies),
        "skipped_sessions": list(skipped_sessions),
    }
    store.put(ARTIFACT_INDEX_KIND, MARKER_KEY, encode(marker))
    logger.info(
        "artifact index migrated reason=artifact_index_migrated records=%d bodies=%d",
        marker["records"],
        marker["bodies"],
    )
    if skipped_sessions:
        logger.warning(
            "artifact index migrated without unreadable sessions "
            "reason=artifact_index_migration_partial skipped_sessions=%s",
            list(skipped_sessions),
        )
    return marker


__all__ = ["INDEX_LAYOUT_VERSION", "index_bodies", "migrate", "read_marker"]
