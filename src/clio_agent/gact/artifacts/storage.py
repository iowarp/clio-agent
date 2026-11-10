"""Provider-neutral artifact byte ingestion and retrieval helpers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from clio_agent.gact.artifacts.cas import IngestedIdentity
from clio_agent.gact.artifacts.provenance.native import NativeArtifactStore
from clio_agent.gact.artifacts.records import Custody

if TYPE_CHECKING:
    from clio_schemas import ArtifactVersion
    from fastapi import FastAPI


_NATIVE_FALLBACK = NativeArtifactStore()


def open_verified_artifact_handle(
    source: Path,
    recorded_sha: str | None,
    *,
    artifact_id: str,
    hash_handle: Callable[[BinaryIO], str],
    error: Any,
) -> BinaryIO:
    """Retain one open, verified object for either streamed or bounded task reads."""
    from clio_agent.platform_paths import win_extended_path

    try:
        handle = open(win_extended_path(source), "rb")
    except OSError as cause:
        raise error(
            status_code=404,
            error="not_found",
            message="artifact bytes are not readable",
            details={"artifact_id": artifact_id},
        ) from cause
    if recorded_sha:
        try:
            actual = hash_handle(handle)
        except OSError as cause:
            handle.close()
            raise error(
                status_code=404,
                error="not_found",
                message="artifact bytes are not readable",
                details={"artifact_id": artifact_id},
            ) from cause
        if actual != recorded_sha:
            handle.close()
            raise error(
                status_code=409,
                error="integrity_violation",
                message="artifact content no longer matches its recorded hash",
                details={
                    "artifact_id": artifact_id,
                    "recorded_sha256": recorded_sha,
                    "actual_sha256": actual,
                },
            )
        handle.seek(0)
    return handle


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
        from clio_agent.gact.artifacts.records import IdentityEvidence

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


def resolve_workspace_state_artifact_path(
    version: "ArtifactVersion", *, workspace_root: Path | None
) -> Path | None:
    """Resolve a hash-pinned reference within this workspace's managed state.

    Materialized source resources live outside the project directory. Their
    advertised artifact references must remain readable without granting access
    to another workspace's state or to arbitrary recorded external paths.
    Callers must verify the recorded hash when opening the returned file.
    """
    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.platform_paths import path_is_within, win_extended_path  # noqa: PLC0415

    if workspace_root is None or not version.path or not version.sha256:
        return None
    source = Path(version.path).expanduser().resolve()
    state_root = paths.workspace_state_dir(workspace_root).resolve()
    if not path_is_within(source, state_root):
        return None
    source = Path(win_extended_path(source))
    return source if source.is_file() else None


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
    A stream (custody ``clio-core``) has no file at all: its content is read through
    its stream route, so the refusal names that route.
    """

    if version.custody == Custody.CLIO_CORE:
        stream_id = str((version.producer or {}).get("stream_id") or "")
        raise error(
            status_code=409,
            error="artifact_is_stream",
            message="a stream's content is read through its stream route",
            details={
                "artifact_id": version.artifact_id,
                "content_path": f"/v1/streams/{stream_id}/content",
            },
        )
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
    "resolve_workspace_state_artifact_path",
]
