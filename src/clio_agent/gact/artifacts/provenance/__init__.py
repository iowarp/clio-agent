"""Artifact-provenance provider and provider-scoped storage interfaces."""

from clio_agent.gact.artifacts.provenance.factory import (
    build_artifact_provenance_backend,
    configured_artifact_provider_name,
)
from clio_agent.gact.artifacts.provenance.protocol import (
    ArtifactProvenanceProvider,
    ArtifactStore,
    StorageReceipt,
)

__all__ = [
    "ArtifactProvenanceProvider",
    "ArtifactStore",
    "StorageReceipt",
    "build_artifact_provenance_backend",
    "configured_artifact_provider_name",
]
