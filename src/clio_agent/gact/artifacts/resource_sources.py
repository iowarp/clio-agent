"""Mint a first-class ``source`` artifact for a ready, materialized resource.

Owner module (no-accretion): the fourth mint seam, alongside
``minting.py``'s (a) tool-declared outputs, (b) approved harness writes, and
(c) pack-declared paths — a user-uploaded resource becomes a citable input the
moment it is materialized into the workspace, at the SAME choke point
:func:`clio_agent.gact.resource_materialization.materialize_once` already owns
for every caller (routes, the agent's own resource tools, and per-turn
attachment enrichment).

Registering it in the SAME artifact registry (CLAUDE.md RULE 4 — no fifth
store) is what makes it resolvable through ``create_artifact``'s declared
``used=[...]`` channel (:mod:`clio_agent.gact.artifacts.declared_used_edges`):
clio never decides or guesses which inputs a deliverable used (the
superseding no-deterministic-decision-making principle) — it only makes a
MODEL-declared reference resolvable, exactly like an ``artifact_<hex>`` id or
a workspace path already are.

The resource<->artifact link lives on the ``ResourceRecord`` itself
(``resource_custody.ResourceRecord.source_artifact_id`` /
``.source_registration``) — an EXISTING store (``resources.json``), never a
new one. The resource record is the natural owner: the link is a fact ABOUT
the resource (whether, and as what, it has been registered), read back from
exactly the same place ``materialization`` already lives, and it is the side
every consumer (enrichment, the used-ref resolver, the migration) already
starts from — a resource id or path, never an artifact id.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactVersion,
    Custody,
    EvidenceClass,
    IdentityEvidence,
    Mechanism,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.resource_custody import ResourceRecord

logger = logging.getLogger(__name__)

#: The ``producer["designation"]`` tag stamped on every source artifact's
#: version — the honest "mechanism or provenance says it was a user upload"
#: signal (records.py's ``Mechanism`` enum, extracted to the cross-repo
#: clio-schemas package, has no ``user-upload`` member; clio-agent does not
#: own that schema, so the designation rides here instead, the SAME pattern
#: every other producer["designation"] tag already uses: "tool-arg",
#: "pack-declared", "reconcile-observed", ...). :func:`is_resource_source`
#: reads it back; the two must never drift, so nothing but this constant
#: writes or compares the tag.
RESOURCE_SOURCE_DESIGNATION = "resource-upload"

#: The presentation-only ``kind`` a source artifact's version SHOWS on the
#: lineage graph (``clio_agent.gact.artifacts.lineage``). The underlying,
#: schema-validated ``ArtifactVersion.kind`` stays ``ArtifactKind.OTHER`` —
#: clio-schemas' curated ``ArtifactKind`` enum (a separate, version-pinned
#: repository) has no ``source`` member, and extending it is out of scope for
#: this slice. This is a real, structural fact clio itself recorded at mint
#: time (never a heuristic guess), surfaced as a derived wire label — the
#: exact same pattern ``lineage._node_type`` already uses to show a
#: mechanism-``none`` version as a ``"gap"`` node without touching
#: ``ArtifactKind``.
SOURCE_PRESENTATION_KIND = "source"


def is_resource_source(version: ArtifactVersion) -> bool:
    """Whether ``version`` is a source artifact minted from an uploaded resource."""

    return str((version.producer or {}).get("designation") or "") == RESOURCE_SOURCE_DESIGNATION


def source_artifact_name(record: "ResourceRecord") -> str:
    """The registry-unique logical name for a resource's source artifact.

    Two different uploads must never fold into the same ``(workspace_id,
    name)`` version chain merely because they share a display name — unlike
    :func:`clio_agent.gact.artifacts.minting.artifact_name_for_path`'s bare
    basename (deliberately reused there so a tool overwriting the SAME
    deliverable revises one chain), a resource's OWN id (unique per upload)
    must be part of the name. The managed working-copy's own workspace-relative
    path already carries that per-resource uniqueness, and doubles as the
    ``path`` a model-cited ``.clio/inputs/<res_id>/<name>`` ref resolves
    against (:mod:`declared_used_edges`) — one name, two jobs, never drifting.
    """
    from clio_agent.gact.resource_materialization import (  # noqa: PLC0415
        managed_input_relative_path,
    )

    return managed_input_relative_path(record).as_posix()


def register_resource_source(app: "FastAPI", record: "ResourceRecord") -> "ResourceRecord":
    """Mint (once) the source artifact for a ready, materialized resource.

    The single entry point, called from
    :func:`clio_agent.gact.resource_materialization.materialize_once` — the
    SAME choke point every caller (routes, resource tools, per-turn
    enrichment) already touches, so this doubles as the one-time migration
    path for a resource that predates this feature (a ``pending``
    ``source_registration`` on first ready-touch), never a second code path.

    Idempotent: a resource already ``registered`` is returned unchanged
    without a second mint; the registry's own same-name identity is ALSO a
    dedup guard should this ever race. Never raises: a failure is recorded as
    a typed ``source_registration`` state on the resource (no-silent-fallback)
    and the resource is returned so the caller can proceed — a citation
    feature must never block materialization.
    """
    from clio_agent.gact.resource_custody import ResourceSourceRegistration  # noqa: PLC0415

    if record.source_registration.state == "registered":
        return record
    if record.materialization.state != "ready" or not record.workspace_path:
        # Not yet materialized (or its copy failed) — nothing to register against.
        return record
    try:
        from clio_agent.gact.artifacts.minting import mint_artifact_outcome  # noqa: PLC0415

        evidence = IdentityEvidence(
            evidence_class=EvidenceClass.HASHED_AT_USE,
            sha256=record.sha256,
            size_bytes=record.declared_size,
        )
        outcome = mint_artifact_outcome(
            app,
            "",
            name=source_artifact_name(record),
            workspace_id=record.workspace_id,
            evidence=evidence,
            kind=ArtifactKind.OTHER,
            mechanism=Mechanism.HARNESS,
            producer={
                "designation": RESOURCE_SOURCE_DESIGNATION,
                "resource_id": record.id,
                "resource_name": record.name,
            },
            custody=Custody.WORKSPACE_REFERENCED,
            path=record.workspace_path,
            annotation=f"User-uploaded source: {record.name}",
            producing=False,
        )
    except Exception as exc:  # noqa: BLE001 - a mint failure must never break materialization
        logger.warning(
            "resource source-artifact registration failed reason=source_registration_failed "
            "workspace_id=%s resource_id=%s error=%s",
            record.workspace_id,
            record.id,
            exc,
        )
        return app.state.resource_store.set_source_registration(
            record.id, ResourceSourceRegistration(state="failed", reason=str(exc))
        )
    if outcome is None:
        logger.warning(
            "resource source-artifact registration failed reason=mint_returned_none "
            "workspace_id=%s resource_id=%s",
            record.workspace_id,
            record.id,
        )
        return app.state.resource_store.set_source_registration(
            record.id, ResourceSourceRegistration(state="failed", reason="mint_returned_none")
        )
    return app.state.resource_store.set_source_registration(
        record.id,
        ResourceSourceRegistration(state="registered"),
        artifact_id=outcome.version.artifact_id,
    )


__all__ = [
    "RESOURCE_SOURCE_DESIGNATION",
    "SOURCE_PRESENTATION_KIND",
    "is_resource_source",
    "register_resource_source",
    "source_artifact_name",
]
