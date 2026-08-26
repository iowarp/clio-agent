"""Contracts for the artifact substream and provider-scoped byte storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from clio_agent.gact.provenance.protocol import ProviderReceipt

if TYPE_CHECKING:
    from clio_schemas import ArtifactVersion

    from clio_agent.gact.artifacts.cas import IngestedIdentity
    from clio_agent.gact.semantic_events import SemanticEvent


@dataclass(frozen=True)
class StorageReceipt:
    """Evidence that one provider-scoped artifact store accepted bytes."""

    provider: str
    backend: str
    object_uri: str
    size_bytes: int
    digests: dict[str, str] = field(default_factory=dict)
    object_name: str = ""
    disposition: str = "stored"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe receipt for the immutable version's producer block."""
        return asdict(self)


@runtime_checkable
class ArtifactStore(Protocol):
    """Primary byte-custody implementation owned by an artifact provider."""

    name: str

    def ingest(self, path: Path, *, workspace_root: Path | None) -> "IngestedIdentity":
        """Stream identity and optionally accept custody of ``path``."""

    def resolve_owned_path(
        self,
        version: "ArtifactVersion",
        *,
        workspace_root: Path | None,
    ) -> Path | None:
        """Return a verified provider-owned local path, or ``None`` if not owned."""


@runtime_checkable
class ArtifactProvenanceProvider(Protocol):
    """Specialized sink/query contract for the artifact event substream."""

    name: str
    durable: bool
    queryable: bool
    store: ArtifactStore

    def emit(self, event: "SemanticEvent") -> ProviderReceipt | None:
        """Record one already-ARC-accepted artifact event."""

    def flush(self) -> None:
        """Block until every already-accepted event is genuinely persisted,
        or honestly return without one.

        This provider is wrapped by the same ``ProvenanceDispatcher`` as
        every agentic provider (via ``ArtifactProvenanceDispatcher``), so the
        REQUIRED contract is identical — see
        :meth:`clio_agent.gact.provenance.protocol.ProvenanceProvider.flush`
        for the three honest shapes a provider may take.
        """

    def lineage(
        self,
        artifact_id: str,
        *,
        direction: str,
        depth: int,
        complete: bool = False,
    ) -> dict[str, Any] | None:
        """Return the normalized CLIO lineage graph for ``artifact_id``."""

    def close(self) -> None:
        """Drain and close provider-owned resources."""
