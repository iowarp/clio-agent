"""The verified bytes of one immutable artifact version, for viewers and comment resolvers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.gact.artifacts.cas import CASStore, sha256_file
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion, Custody

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["ArtifactBytesError", "artifact_bytes_path"]


class ArtifactBytesError(RuntimeError):
    """The version's bytes cannot be served; ``code`` is the typed reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def artifact_bytes_path(app: "FastAPI", record: ArtifactRecord, version: ArtifactVersion) -> Path:
    """The file holding ``version``'s exact bytes, verified against its SHA-256.

    Raises:
        ArtifactBytesError: ``artifact_bytes_unavailable`` when no file holds the bytes,
            ``integrity_violation`` when the file no longer matches the version's hash.
    """

    workspace = app.state.workspaces.get(record.workspace_id)
    workspace_root = Path(str(getattr(workspace, "root_path", "") or "")).resolve(strict=False)
    if version.custody == Custody.CAS and version.sha256:
        cas_candidate = CASStore(workspace_root).blob_path(version.sha256)
        if cas_candidate.is_file():
            return cas_candidate
    candidate = Path(version.path) if version.path else None
    if candidate is None or not candidate.is_file():
        raise ArtifactBytesError("artifact_bytes_unavailable", "artifact bytes are unavailable")
    if version.sha256 and sha256_file(candidate) != version.sha256:
        raise ArtifactBytesError(
            "integrity_violation", "artifact bytes do not match the immutable version hash"
        )
    return candidate
