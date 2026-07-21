"""Artifact record model — enums, immutable versions, logical chains, evidence.

The record shape is relay-``ArtifactRef``-compatible (owner decision #966.3): each
immutable version carries its own relay-format ``artifact_id``
(``artifact_<uuid4hex>``) and a content ``sha256``, so relay's
``ArtifactUse {artifact_id, sha256}`` edges land unchanged when federation later
swaps executors. A logical artifact is keyed ``(workspace_id, name)`` so version
chains survive session boundaries.

Everything here is a pure Pydantic model — no I/O, no app state. The harness
computes hashes and builds these records; the model is never load-bearing in the
chain of custody (design §2.1). Model-provided intent is quarantined in
``annotation``.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ArtifactKind(str, Enum):
    """The curated kinds an artifact version may take (owner decision #966.4).

    ``plan`` is RESERVED for a future planning capability — nothing mints it this
    campaign (:data:`RESERVED_KINDS` + the mint-time guard enforce that).
    ``ui_payload`` (mcpui/a2ui) is record + delivery only; rendering belongs to
    the later mcpui campaign.
    """

    DATASET = "dataset"
    IMAGE = "image"
    REPORT = "report"
    PLAN = "plan"
    SCRIPT = "script"
    CONFIG = "config"
    MODEL = "model"
    UI_PAYLOAD = "ui_payload"
    OTHER = "other"


#: Kinds no mechanism may mint in this campaign. Minting one is a programming
#: error (a reserved capability leaked), surfaced as a typed ``ValueError`` at the
#: mint boundary rather than silently downgraded.
RESERVED_KINDS: frozenset[ArtifactKind] = frozenset({ArtifactKind.PLAN})


class Custody(str, Enum):
    """Where the bytes live and what guarantee that custody class provides.

    Detection (hash self-validation) is the universal guarantee; prevention is
    per-class (design §3, resolution 5a). Campaign B adds ``isolated``.
    """

    CAS = "cas"  # sha256-addressed under the app-owned store (app-private)
    WORKSPACE_REFERENCED = "workspace-referenced"  # bytes stay in the workspace
    EXTERNAL_REFERENCED = "external-referenced"  # bytes external, identity pinned


class Mechanism(str, Enum):
    """What produced a record (design §4 — a set of mechanisms, NOT a ladder).

    Never a single confidence scalar; per-edge evidence rides
    :class:`IdentityEvidence` separately.
    """

    HARNESS = "harness"  # the harness executed the write (fs_write, staged download)
    TOOL_SCHEMA = "tool-schema"  # MCP tool declared out, hash-verified at the hook
    CHANGE_FEED = "change-feed"  # territory-scoped file event under a lease
    MODEL = "model"  # assertion only, quarantined
    NONE = "none"  # detected, unattributed change (gap)


class EvidenceClass(str, Enum):
    """How a version's identity is known (design §3 — identity evidence classes)."""

    HASHED_AT_USE = "hashed-at-use"  # locally computed sha256
    AUTHORITY_ASSERTED = "authority-asserted"  # DOI / registry checksum / ETag
    STAT_PINNED = "stat-pinned"  # size+mtime only; weakest, permanently labeled


class IdentityEvidence(BaseModel):
    """Per-version identity evidence: the basis on which its content is pinned.

    Split from ``mechanism`` deliberately (design resolution 6): the mechanism
    says what produced the record; the evidence class says how the content
    identity is known. A ``stat-pinned`` version carries no ``sha256`` — the
    label is permanent, never silently upgraded.
    """

    model_config = ConfigDict(frozen=True)

    evidence_class: EvidenceClass
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    mtime: Optional[float] = None
    #: For ``authority-asserted``: the authority reference (DOI / registry id / ETag).
    authority: str = ""

    @classmethod
    def hashed_at_use(
        cls, *, sha256: str, size_bytes: int, mtime: float | None = None
    ) -> "IdentityEvidence":
        """Locally computed sha256 — the strongest local class."""
        return cls(
            evidence_class=EvidenceClass.HASHED_AT_USE,
            sha256=sha256,
            size_bytes=size_bytes,
            mtime=mtime,
        )

    @classmethod
    def stat_pinned(cls, *, size_bytes: int, mtime: float | None = None) -> "IdentityEvidence":
        """Size+mtime only — the weakest class, e.g. a file over the hash threshold."""
        return cls(
            evidence_class=EvidenceClass.STAT_PINNED,
            size_bytes=size_bytes,
            mtime=mtime,
        )

    @classmethod
    def authority_asserted(
        cls, *, authority: str, sha256: str | None = None, size_bytes: int | None = None
    ) -> "IdentityEvidence":
        """DOI / registry checksum / ETag — often stronger than local hashing."""
        return cls(
            evidence_class=EvidenceClass.AUTHORITY_ASSERTED,
            authority=authority,
            sha256=sha256,
            size_bytes=size_bytes,
        )


def new_artifact_id() -> str:
    """Return a fresh relay-format artifact id (``artifact_<uuid4hex>``).

    One per immutable version — relay's ``ArtifactRef``/``ArtifactUse`` edges key
    on this exact shape (owner decision #966.3).
    """
    return f"artifact_{uuid.uuid4().hex}"


class ArtifactVersion(BaseModel):
    """One immutable version of a logical artifact.

    Carries its own relay-format ``artifact_id`` and content ``sha256`` (when the
    evidence class provides one). ``producer`` records the producing call identity
    (call_id / session_id / turn_id) — the ``b = transform(a)`` seam a later slice
    builds full TransformRecords on. ``annotation`` quarantines model-provided
    intent; it is never merged into custody/evidence.
    """

    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(default_factory=new_artifact_id)
    version: int = 1
    kind: ArtifactKind = ArtifactKind.OTHER
    custody: Custody = Custody.WORKSPACE_REFERENCED
    mechanism: Mechanism = Mechanism.TOOL_SCHEMA
    evidence: IdentityEvidence
    #: The producing activity identity: ``{call_id, session_id, turn_id, tool}``.
    producer: dict[str, Any] = Field(default_factory=dict)
    #: The referenced path (workspace-referenced custody) — never the source of truth.
    path: str = ""
    created_at: str = ""
    #: Model-provided intent (deliverable-vs-scratch, a label). Untrusted (§2.1).
    annotation: str = ""

    @property
    def sha256(self) -> Optional[str]:
        """Convenience: the content hash, or ``None`` for a stat-pinned version."""
        return self.evidence.sha256

    @property
    def size_bytes(self) -> Optional[int]:
        """Convenience: the recorded byte size."""
        return self.evidence.size_bytes

    def to_artifact_ref(self) -> dict[str, Any]:
        """Project to the relay ``ArtifactRef`` / ``ArtifactUse`` edge shape.

        ``{artifact_id, sha256}`` is the exact pair relay's PROV-style ``used``
        edges key on; the extra fields ride ``metadata`` until relay's schema
        converges (design §6.3).
        """
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "metadata": {
                "kind": self.kind.value,
                "version": self.version,
                "custody": self.custody.value,
                "mechanism": self.mechanism.value,
                "evidence_class": self.evidence.evidence_class.value,
            },
        }


class ArtifactRecord(BaseModel):
    """A logical artifact — the version chain keyed ``(workspace_id, name)``.

    Workspace-scoped so chains survive session boundaries (owner decision
    #966.3). Versions are append-only and immutable; ``aliases`` are mutable
    pointers into the chain (``latest`` is maintained automatically).
    """

    model_config = ConfigDict(frozen=False)

    workspace_id: str
    name: str
    versions: list[ArtifactVersion] = Field(default_factory=list)
    #: Mutable alias -> version number (``latest`` always tracks the head).
    aliases: dict[str, int] = Field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        """The logical identity: ``(workspace_id, name)``."""
        return (self.workspace_id, self.name)

    @property
    def kind(self) -> ArtifactKind:
        """The kind of the head version (``other`` for an empty chain)."""
        head = self.head
        return head.kind if head is not None else ArtifactKind.OTHER

    @property
    def head(self) -> Optional[ArtifactVersion]:
        """The newest version, or ``None`` for an empty chain."""
        return self.versions[-1] if self.versions else None

    def version_for_sha(self, sha256: str | None) -> Optional[ArtifactVersion]:
        """Return an existing version whose content hash matches, else ``None``.

        Used for W&B-style dedup (same name + same hash → no new version). A
        ``None`` sha (stat-pinned) never dedups — identity is unknown.
        """
        if not sha256:
            return None
        for ver in self.versions:
            if ver.sha256 == sha256:
                return ver
        return None

    def add_version(self, version: ArtifactVersion) -> ArtifactVersion:
        """Append ``version`` as the new head and move the ``latest`` alias.

        The caller sets ``version.version``; this only maintains the chain + the
        ``latest`` alias. Returns the appended version.
        """
        self.versions.append(version)
        self.aliases["latest"] = version.version
        return version

    def next_version_number(self) -> int:
        """The version number a new head would take (1-based)."""
        return (self.head.version + 1) if self.head is not None else 1
