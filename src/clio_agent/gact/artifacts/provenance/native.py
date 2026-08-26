"""Native CLIO artifact graph and filesystem-CAS provider."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.cas import CASStore, ingest_identity
from clio_agent.gact.artifacts.lineage import build_lineage
from clio_agent.gact.artifacts.records import Custody
from clio_agent.gact.provenance.protocol import ProviderReceipt

if TYPE_CHECKING:
    from clio_schemas import ArtifactVersion
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.cas import IngestedIdentity
    from clio_agent.gact.artifacts.provenance.protocol import ArtifactStore
    from clio_agent.gact.semantic_events import SemanticEvent


class NativeArtifactStore:
    """The existing SHA-256 filesystem CAS behind the artifact-store contract."""

    name = "file"

    def ingest(self, path: Path, *, workspace_root: Path | None) -> "IngestedIdentity":
        """Use the existing single-pass CLIO identity/CAS ingestion path."""
        return ingest_identity(path, workspace_root=workspace_root)

    def resolve_owned_path(
        self,
        version: "ArtifactVersion",
        *,
        workspace_root: Path | None,
    ) -> Path | None:
        """Resolve a present CAS blob; referenced workspace bytes are not owned."""
        if workspace_root is None or version.custody is not Custody.CAS or not version.sha256:
            return None
        blob = CASStore(workspace_root).blob_path(version.sha256)
        return blob if blob.is_file() else None


class NativeArtifactProvenanceProvider:
    """Adapter over CLIO's ARC-derived registry and lineage builder."""

    name = "native"
    durable = True
    queryable = True

    def __init__(self, app: "FastAPI") -> None:
        self._app = app
        self.store: ArtifactStore = NativeArtifactStore()

    def emit(self, event: "SemanticEvent") -> ProviderReceipt:
        """Acknowledge the event already folded by ARC's artifact observer."""
        del event
        return ProviderReceipt.ACCEPTED

    def flush(self) -> None:
        """No-op, and honestly a complete barrier: :meth:`emit` only
        acknowledges an event ARC already folded SYNCHRONOUSLY (see its
        docstring) -- there is no further async write behind this provider
        for flush() to drain."""
        return

    def lineage(
        self,
        artifact_id: str,
        *,
        direction: str,
        depth: int,
        complete: bool = False,
    ) -> dict[str, Any] | None:
        """Build the established normalized graph from the live registry."""
        from clio_agent.gact.artifacts.registry import get_registry

        return build_lineage(
            get_registry(self._app),
            artifact_id,
            direction=direction,
            depth=depth,
            complete=complete,
        )

    def close(self) -> None:
        """Native registry/store resources share the app lifecycle."""


__all__ = ["NativeArtifactProvenanceProvider", "NativeArtifactStore"]
