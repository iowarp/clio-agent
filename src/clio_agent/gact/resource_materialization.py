"""Materialize immutable uploaded resources as mutable workspace inputs.

Custody remains authoritative for hashes, revisions, conversion, and provenance.
The copy under ``.clio/inputs`` exists so workspace-scoped filesystem tools can
consume or transform an upload without ever mutating the custody original.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.resource_custody import ResourceRecord, ResourceStore

MANAGED_INPUT_DIRECTORY = Path(".clio") / "inputs"


def managed_input_relative_path(record: "ResourceRecord") -> Path:
    """Return the collision-safe workspace-relative path for ``record``."""

    if not record.id.startswith("res_") or not record.id.removeprefix("res_").isalnum():
        raise ValueError("resource id is not safe for workspace materialization")
    if Path(record.name).name != record.name:
        raise ValueError("resource name is not safe for workspace materialization")
    return MANAGED_INPUT_DIRECTORY / record.id / record.name


def materialize_resource(
    store: "ResourceStore", record: "ResourceRecord", workspace_root: str | Path
) -> "ResourceRecord":
    """Atomically copy a ready custody original into its active workspace.

    This deliberately uses a byte copy rather than a hardlink. The workspace
    input is a mutable working copy; editing it must not alter immutable custody.
    """

    source = store.content_path(record)
    root = Path(workspace_root).expanduser().resolve(strict=False)
    relative = managed_input_relative_path(record)
    destination = (root / relative).resolve(strict=False)
    destination.relative_to(root)

    recorded = Path(record.workspace_path).resolve(strict=False) if record.workspace_path else None
    if recorded == destination and destination.is_file():
        return record

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    updated = store.set_workspace_path(record.id, str(destination))
    if recorded is not None and recorded != destination:
        remove_materialized_resource(record)
    return updated


def materialize_resource_for_app(app: Any, record: "ResourceRecord") -> "ResourceRecord":
    """Materialize ``record`` under the root registered on its owning app."""

    workspace = app.state.workspaces.get(record.workspace_id)
    root = str(getattr(workspace, "root_path", "") or "") if workspace is not None else ""
    if not root:
        raise ValueError(f"workspace has no registered root: {record.workspace_id}")
    return materialize_resource(app.state.resource_store, record, root)


def remove_materialized_resource(record: "ResourceRecord") -> None:
    """Remove only the validated managed directory for ``record`` if present."""

    if not record.workspace_path:
        return
    target = Path(record.workspace_path).expanduser().resolve(strict=False)
    resource_dir = target.parent
    inputs_dir = resource_dir.parent
    clio_dir = inputs_dir.parent
    if (
        target.name != record.name
        or resource_dir.name != record.id
        or inputs_dir.name != "inputs"
        or clio_dir.name != ".clio"
    ):
        raise ValueError("recorded workspace input path is outside the managed layout")
    if resource_dir.exists():
        shutil.rmtree(resource_dir)
    if inputs_dir.exists() and not any(inputs_dir.iterdir()):
        inputs_dir.rmdir()


__all__ = [
    "MANAGED_INPUT_DIRECTORY",
    "managed_input_relative_path",
    "materialize_resource",
    "materialize_resource_for_app",
    "remove_materialized_resource",
]
