"""Provider-neutral artifact byte ingestion and retrieval helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.cas import IngestedIdentity
from clio_agent.gact.artifacts.provenance.native import NativeArtifactStore

if TYPE_CHECKING:
    from clio_schemas import ArtifactVersion
    from fastapi import FastAPI


_NATIVE_FALLBACK = NativeArtifactStore()


def artifact_store(app: "FastAPI") -> Any:
    """Return the selected provider's store, with native fallback for test apps."""
    backend = getattr(app.state, "artifact_provenance_backend", None)
    store = getattr(backend, "store", None)
    return store or _NATIVE_FALLBACK


def ingest_artifact_identity(
    app: "FastAPI",
    path: str | Path,
    *,
    workspace_root: Path | None,
) -> IngestedIdentity:
    """Ingest through the selected artifact provider's primary store."""
    return artifact_store(app).ingest(Path(path), workspace_root=workspace_root)


def harness_write_artifact_identity(
    app: "FastAPI",
    path: str | Path,
    *,
    workspace_root: Path | None,
    in_hand_sha: str,
    in_hand_size: int,
) -> IngestedIdentity:
    """Ingest a just-written artifact, falling back to its writer-held identity."""
    try:
        return ingest_artifact_identity(app, path, workspace_root=workspace_root)
    except OSError:
        if not in_hand_sha:
            raise
        from clio_agent.gact.artifacts.records import Custody, IdentityEvidence

        return IngestedIdentity(
            evidence=IdentityEvidence.hashed_at_use(
                sha256=in_hand_sha,
                size_bytes=in_hand_size,
                mtime=None,
            ),
            custody=Custody.WORKSPACE_REFERENCED,
            reason="harness_ingest_failed",
        )


def resolve_owned_artifact_path(
    app: "FastAPI",
    version: "ArtifactVersion",
    *,
    workspace_root: Path | None,
) -> Path | None:
    """Resolve verified bytes owned by the selected provider, if any."""
    return artifact_store(app).resolve_owned_path(version, workspace_root=workspace_root)


def resolve_owned_artifact_or_raise(
    app: "FastAPI",
    version: "ArtifactVersion",
    *,
    workspace_root: Path | None,
    error: Any,
) -> Path | None:
    """Provider-owned bytes, or a typed refusal when custody is broken.

    ``None`` means "no provider-owned object — fall through to the workspace
    reference ladder". But a version whose producer carries a CMF
    ``storage_receipt`` with NO resolvable object is a broken custody chain,
    not a fall-through: raise the caller-built typed 409
    ``artifact_store_unavailable`` (``error`` is the route's HTTPException
    factory — kept a seam so this module never imports route machinery).
    """

    owned = resolve_owned_artifact_path(app, version, workspace_root=workspace_root)
    if owned is not None:
        return owned
    receipt = (version.producer or {}).get("storage_receipt")
    if isinstance(receipt, dict) and receipt.get("provider") == "cmf":
        raise error(
            status_code=409,
            error="artifact_store_unavailable",
            message="CMF has custody but its recorded artifact object is unavailable",
            details={
                "artifact_id": version.artifact_id,
                "provider": "cmf",
                "object_uri": str(receipt.get("object_uri") or ""),
            },
        )
    return None


__all__ = [
    "artifact_store",
    "harness_write_artifact_identity",
    "ingest_artifact_identity",
    "resolve_owned_artifact_path",
]
