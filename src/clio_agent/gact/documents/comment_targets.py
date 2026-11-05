"""Comment on any workspace file: register it as an artifact version when it is not one.

The person can open a file from the workspace's file tree and comment on it there, whether or
not the agent registered it (ledger FB22). A file that is already a version of an artifact is
used as it is, after its path is reconciled so changed bytes become the next version (FB28).
A file that is not is registered on demand with the designation ``comment-target`` and no
producing call, so its entries say the producer is unknown. Artifacts are named by basename;
when another file already holds that name, the new artifact is named by its workspace-relative
path instead, so a comment never lands on another file's history (FB29).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion, Mechanism
from clio_agent.gact.artifacts.registry import get_registry

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["CommentTargetError", "find_target", "register_target"]


class CommentTargetError(RuntimeError):
    """The file cannot be a comment target; ``code`` is typed and ``status`` its HTTP status."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _resolve(app: "FastAPI", workspace_id: str, path: str) -> tuple[Path, Path]:
    from clio_agent.gact.artifacts.minting import _contained, _workspace_root  # noqa: PLC0415

    root = _workspace_root(app, workspace_id)
    if root is None:
        raise CommentTargetError(404, "workspace_not_found", f"workspace not found: {workspace_id}")
    candidate = Path(path)
    absolute = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=False)
    if not _contained(absolute, root):
        raise CommentTargetError(403, "path_outside_workspace", "the file is outside the workspace")
    if not absolute.is_file():
        raise CommentTargetError(404, "file_not_found", f"no file at {path}")
    return root, absolute


def find_target(
    app: "FastAPI", workspace_id: str, path: str
) -> tuple[ArtifactRecord, ArtifactVersion] | None:
    """The artifact version a workspace file is, without registering it."""

    _root, absolute = _resolve(app, workspace_id, path)
    return get_registry(app).find_version_by_path(workspace_id, str(absolute))


def _name(app: "FastAPI", workspace_id: str, root: Path, absolute: Path) -> str:
    from clio_agent.gact.artifacts.minting import artifact_name_for_path  # noqa: PLC0415

    name = artifact_name_for_path(absolute)
    existing = get_registry(app).get(workspace_id, name)
    if existing is None or existing.head is None or not existing.head.path:
        return name
    if Path(existing.head.path).resolve(strict=False) == absolute:
        return name
    return absolute.relative_to(root).as_posix()


def register_target(
    app: "FastAPI", session_id: str, workspace_id: str, path: str
) -> tuple[ArtifactRecord, ArtifactVersion]:
    """The artifact version to comment on for a workspace file, registering it if needed.

    Raises:
        CommentTargetError: the workspace or file is unknown, or the file is outside it.
    """

    from clio_agent.gact.artifacts.designation import kind_for_path  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import mint_artifact  # noqa: PLC0415
    from clio_agent.gact.artifacts.storage import ingest_artifact_identity  # noqa: PLC0415
    from clio_agent.gact.artifacts.versions import reconcile_designated_path  # noqa: PLC0415

    root, absolute = _resolve(app, workspace_id, path)
    registry = get_registry(app)
    found = registry.find_version_by_path(workspace_id, str(absolute))
    if found is not None:
        record, _version = found
        reconcile_designated_path(
            app,
            session_id,
            name=record.name,
            workspace_id=workspace_id,
            path=str(absolute),
            mechanism=Mechanism.CHANGE_FEED,
            session_id=session_id,
        )
        current = registry.get(workspace_id, record.name)
        if current is None or current.head is None:
            raise CommentTargetError(409, "artifact_chain_missing", "the artifact has no versions")
        return current, current.head
    ingested = ingest_artifact_identity(app, absolute, workspace_root=root)
    version = mint_artifact(
        app,
        session_id,
        name=_name(app, workspace_id, root, absolute),
        workspace_id=workspace_id,
        evidence=ingested.evidence,
        kind=kind_for_path(absolute),
        mechanism=Mechanism.NONE,
        producer={"designation": "comment-target", "session_id": session_id},
        custody=ingested.custody,
        path=str(absolute),
        ingested=ingested,
        not_ingested_size=ingested.not_ingested_size,
    )
    if version is None:
        raise CommentTargetError(409, "artifact_not_registered", "the file could not be registered")
    resolved = registry.get_by_artifact_id(version.artifact_id)
    if resolved is None:
        raise CommentTargetError(409, "artifact_not_registered", "the file could not be registered")
    return resolved
