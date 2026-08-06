"""Dedup-enrichment side index — the decision logic behind A9 (#1176).

Split out of :mod:`clio_agent.gact.artifacts.registry` (no-accretion ground rule,
registry.py is already at its per-file ratchet ceiling): the SAME split
``versions.decide_version`` uses for ``ArtifactRegistry.mint`` — the registry class
owns the lock + the dict, this module owns the pure decision made under that lock.

A ``create_artifact`` call that hits ``already_registered`` (W&B same-sha dedup)
still names a real fact the caller declared: a description the deduped-onto
version may be missing. That version is an IMMUTABLE ``clio_schemas.ArtifactVersion``
(owned upstream, never mutable in place), so the description cannot be written onto
it directly — it is attached here to a side index keyed by ``artifact_id`` instead,
and the route wire (``routes/artifacts.py::_version_wire``) merges it in wherever the
version's own ``annotation`` is blank. First-caller-wins: an already-annotated
version (its own, or an earlier supplemental one) is never silently overwritten —
the typed ``"annotation_already_present"`` reason says so instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.records import ArtifactVersion
    from clio_agent.gact.artifacts.registry import ArtifactRegistry

logger = logging.getLogger(__name__)


def merged_annotation(version: "ArtifactVersion", registry: "ArtifactRegistry | None") -> str:
    """The version's own ``annotation``, or its supplemental one if blank (A9).

    The route wire boundary (``routes/artifacts.py::_version_wire``) calls this
    for every projected version instead of inlining the registry lookup — kept
    here, not there, so the route file (already at its ratchet ceiling) gains no
    decision logic, only a call. ``registry=None`` (no registry in scope) skips
    the merge, never a crash.
    """
    if version.annotation or registry is None:
        return version.annotation
    return registry.supplemental_annotation(version.artifact_id)


#: The typed outcomes :func:`decide_enrichment` returns — never a silent drop.
MERGED = "merged"
NO_ANNOTATION_GIVEN = "no_annotation_given"
UNKNOWN_ARTIFACT = "unknown_artifact"
ANNOTATION_ALREADY_PRESENT = "annotation_already_present"
DUPLICATE_EVENT_ID = "duplicate_event_id"


def decide_enrichment(
    registry: "ArtifactRegistry", artifact_id: str, annotation: str, event_id: str
) -> str:
    """Decide + apply the enrichment for ``artifact_id``. Caller holds ``registry._lock``.

    Reaches into the registry's private dicts directly (the SAME cross-module
    convention ``transform_edges``/``versions`` already use for ``minting._contained``
    / ``minting._workspace_root`` — this pairing is a tightly-coupled owner-module
    split, not a public API).
    """
    if not annotation:
        return NO_ANNOTATION_GIVEN
    if event_id and event_id in registry._seen_event_ids:  # noqa: SLF001
        return DUPLICATE_EVENT_ID
    version = None
    for record in registry._records.values():  # noqa: SLF001
        version = next((v for v in record.versions if v.artifact_id == artifact_id), None)
        if version is not None:
            break
    if version is None:
        return UNKNOWN_ARTIFACT
    if event_id:
        registry._seen_event_ids.add(event_id)  # noqa: SLF001
    if version.annotation or artifact_id in registry._supplemental_annotations:  # noqa: SLF001
        return ANNOTATION_ALREADY_PRESENT
    registry._supplemental_annotations[artifact_id] = annotation  # noqa: SLF001
    return MERGED


def emit_artifact_enriched(
    app: "FastAPI",
    sid: str,
    *,
    workspace_id: str,
    name: str,
    version: "ArtifactVersion",
    annotation: str,
    turn_id: str = "",
    trace_id: str = "",
) -> str:
    """Emit + materialize the durable ``artifact.enriched`` event (A9, #1176).

    Mirrors :func:`~clio_agent.gact.artifacts.versions.emit_artifact_used`'s shape
    (a fresh event id; the registry write decides before anything is emitted).
    Returns the typed reason from :meth:`ArtifactRegistry.record_artifact_enrichment`
    (module constants above), or ``"enrichment_emit_failed"`` on the guarded
    exception path — a wire emit must never break a live dedup, but the caller
    still gets an honest reason instead of a swallowed one. Trace-only.
    """
    try:
        from clio_agent.gact.artifacts.registry import (  # noqa: PLC0415
            ARTIFACT_ENRICHED_EVENT,
            get_registry,
        )
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415
        from clio_agent.gact.semantic_events import _event_id  # noqa: PLC0415

        event_id = _event_id()
        reason = get_registry(app).record_artifact_enrichment(
            version.artifact_id, annotation=annotation, event_id=event_id
        )
        if reason != MERGED:
            return reason
        subject = {"artifact_id": version.artifact_id, "name": name, "workspace_id": workspace_id}
        _emit_semantic_event(
            app,
            sid,
            ARTIFACT_ENRICHED_EVENT,
            turn_id=turn_id,
            trace_id=trace_id,
            status="completed",
            summary=f"Session {sid} attached a description to artifact {name} v{version.version}.",
            actor={"session_id": sid, "mechanism": "model"},
            subject=subject,
            payload={
                **subject,
                "event_id": event_id,
                "version": version.version,
                "session_id": sid,
                "annotation": annotation,
            },
            detail_level="semantic",
        )
        return reason
    except Exception:  # noqa: BLE001 — a wire emit must never break a live dedup
        logger.warning(
            "artifact enrichment emit skipped reason=enrichment_emit_failed session=%s name=%s",
            sid,
            name,
        )
        return "enrichment_emit_failed"


__all__ = [
    "ANNOTATION_ALREADY_PRESENT",
    "DUPLICATE_EVENT_ID",
    "MERGED",
    "NO_ANNOTATION_GIVEN",
    "UNKNOWN_ARTIFACT",
    "decide_enrichment",
    "emit_artifact_enriched",
    "merged_annotation",
]
