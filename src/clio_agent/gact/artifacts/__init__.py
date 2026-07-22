"""First-class session artifacts — record model, registry projection, minting floor.

Campaign A (artifacts) slice S1. This package owns the artifact record model
(:mod:`records`), the in-memory registry projection + boot fold + SessionStore
small-index patcher (:mod:`registry`), and the tool-declared designation table
(:mod:`designation`) that says which output paths mint.

Design authority: ``docs/design/artifact-provenance-design.md`` (v2) and the
owner-locked decisions in issue #966. Storage is a PROJECTION over the existing
``_emit_semantic_event`` log (RULE 4 / #737) — this package adds no new store.
Events are trace-only this slice; the wire lights up in S2 (#968).
"""

from __future__ import annotations

from clio_agent.gact.artifacts.designation import (
    ARTIFACT_SUFFIXES,
    OUTPUT_PATH_ARG_NAMES,
    ground_output_paths,
    kind_for_path,
)
from clio_agent.gact.artifacts.minting import (
    compute_identity,
    drain_turn_artifacts,
    hash_max_file_bytes,
    mint_artifact,
    mint_pack_declared_paths,
    mint_tool_declared_outputs,
)
from clio_agent.gact.artifacts.proposals import (
    Proposal,
    ProposalOutcome,
    RejectionReason,
    build_create_artifact_tool,
    promote_proposal,
    promote_proposals,
    proposals_per_turn,
    validate_kind,
)
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Custody,
    EvidenceClass,
    IdentityEvidence,
    Mechanism,
    new_artifact_id,
)
from clio_agent.gact.artifacts.registry import (
    ArtifactRegistry,
    FoldResult,
    get_registry,
    rebuild_registry_at_boot,
)
from clio_agent.gact.artifacts.wire import (
    artifact_uri,
    resource_link_part,
    ui_payload_uri,
)

__all__ = [
    "ARTIFACT_SUFFIXES",
    "OUTPUT_PATH_ARG_NAMES",
    "ArtifactKind",
    "ArtifactRecord",
    "ArtifactRegistry",
    "ArtifactVersion",
    "Custody",
    "EvidenceClass",
    "FoldResult",
    "IdentityEvidence",
    "Mechanism",
    "Proposal",
    "ProposalOutcome",
    "RejectionReason",
    "artifact_uri",
    "build_create_artifact_tool",
    "compute_identity",
    "drain_turn_artifacts",
    "get_registry",
    "ground_output_paths",
    "hash_max_file_bytes",
    "kind_for_path",
    "mint_artifact",
    "mint_pack_declared_paths",
    "mint_tool_declared_outputs",
    "new_artifact_id",
    "promote_proposal",
    "promote_proposals",
    "proposals_per_turn",
    "rebuild_registry_at_boot",
    "resource_link_part",
    "ui_payload_uri",
    "validate_kind",
]
