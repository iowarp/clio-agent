"""Leaf value types for TransformRecords (S5 #971) — enums, edges, instrument.

Split out of :mod:`clio_agent.gact.artifacts.transforms` so both the record
orchestration (``transforms``) and the edge detection (``transform_edges``) share
one dependency-free type module (no import cycle). Pure Pydantic / enums — no I/O,
no app state.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class EdgeRole(str, Enum):
    """Which side of the transform an edge sits on (PROV ``used`` / ``generated``)."""

    USED = "used"
    GENERATED = "generated"


class EdgeEvidence(str, Enum):
    """How a provenance edge's identity is known (owner decision #966.5).

    Per-edge, never a single confidence scalar. ``schema-arg`` — the path came
    from a declared arg; ``hash-pair`` — content re-hashed and matched (or gap-
    minted) a registry version; ``lease-window`` — attribution under a clean
    single-writer lease; ``authority`` — a catalog URL/registry id/DOI/ETag;
    ``assertion`` — model-quarantined intent only (weakest).
    """

    SCHEMA_ARG = "schema-arg"
    HASH_PAIR = "hash-pair"
    LEASE_WINDOW = "lease-window"
    AUTHORITY = "authority"
    ASSERTION = "assertion"


class TransformStatus(str, Enum):
    """Whether the producing call succeeded (a failed run that wrote is provenance)."""

    SUCCESS = "success"
    FAILED = "failed"


class AgentRole(str, Enum):
    """Whether the agent EXECUTED the transform or merely ANNOTATED an existing one."""

    EXECUTING = "executing"
    ANNOTATING = "annotating"


class TransformKind(str, Enum):
    """Ordinary vs a ``contended`` record (owner decision #966.10).

    ``ordinary`` — the per-workspace-root executor lock proved a single writer for
    this window. ``contended`` — another active session/task could be writing the
    same workspace, so generated attribution rides a candidate set, never a false
    certainty.
    """

    ORDINARY = "ordinary"
    CONTENDED = "contended"


class ReplayContract(str, Enum):
    """The permanent, honest replay guarantee stamped on a record (owner decision #966.6).

    ``reproducible`` — environment tier ≥ ``lockfile-hash`` AND every used input
    is content-pinned, so a bit-identical replay is guaranteed. ``re-runnable`` —
    the run is fully described but one of those conditions fails; re-running may
    not be bit-identical. Never silently upgraded.
    """

    REPRODUCIBLE = "reproducible"
    RE_RUNNABLE = "re-runnable"


class ProvEdge(BaseModel):
    """One ``used`` / ``generated`` provenance edge with its own evidence.

    A registry-matched edge carries the relay ``artifact_id`` + ``sha256`` (the
    exact :class:`~clio_agent.gact.artifacts.records.ArtifactVersion` pair relay's
    ``ArtifactUse`` keys on); an external edge carries ``external_ref``
    (``external:<path>`` or a catalog URL) instead. ``evidence`` is the per-edge
    basis; ``note`` records a typed qualifier (``gap_first`` / ``stat_pinned`` /
    ``over_threshold`` / ``relink``) so no downgrade is silent.
    """

    model_config = ConfigDict(frozen=True)

    role: EdgeRole
    evidence: EdgeEvidence
    #: The relay ``artifact_id`` when this edge points at a registered version.
    artifact_id: str = ""
    sha256: Optional[str] = None
    #: ``external:<path>`` or a catalog URL for an edge NOT in the registry.
    external_ref: str = ""
    #: For ``authority`` evidence: the asserting reference (catalog URL / DOI / id).
    authority: str = ""
    name: str = ""
    version: Optional[int] = None
    path: str = ""
    #: The call arg the edge was discovered on (``""`` for result-derived edges).
    arg: str = ""
    note: str = ""

    def to_artifact_use(self) -> Optional[dict[str, Any]]:
        """Project to the relay ``ArtifactUse {artifact_id, sha256}`` shape, or ``None``.

        Only a registry-matched edge WITH a content hash is a valid relay
        ``ArtifactUse`` (relay requires a 64-hex sha). A stat-pinned or purely
        external/authority edge has no sha and rides ``metadata`` instead — the
        exact convergence gap the clio-relay issue proposes closing.
        """
        if not self.artifact_id or not self.sha256:
            return None
        return {"artifact_id": self.artifact_id, "sha256": self.sha256}


class Instrument(BaseModel):
    """WHAT produced the transform — a tool call, or a ``{cmd, script_hash}`` script.

    A generated script that a shell/exec tool runs is itself a ``script``-kind
    artifact and its own hashed dependency (DVC's move): ``script_hash`` +
    ``script_artifact_id`` pin it, and it ALSO appears as a used edge.
    """

    model_config = ConfigDict(frozen=True)

    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    cmd: str = ""
    script_hash: str = ""
    script_artifact_id: str = ""


__all__ = [
    "AgentRole",
    "EdgeEvidence",
    "EdgeRole",
    "Instrument",
    "ProvEdge",
    "ReplayContract",
    "TransformKind",
    "TransformStatus",
]
