"""Expose approved read-only source snapshots in the existing workspace Files view."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from clio_agent.gact.resource_mime import detect_media_type
from clio_agent.gact.storage.adapters import safe_child
from clio_agent.gact.storage.models import Manifest
from clio_agent.gact.storage.service import StorageService

_PREFIX = PurePosixPath(".clio-agent/sources")


def connected_file_entries(
    service: StorageService, workspace_id: str, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Project recorded snapshots without copying files or scanning credential storage."""
    from clio_agent.gact.routes.workspace_file_listing import workspace_file_media_type

    entries: list[dict[str, Any]] = []
    for record in service.sources(workspace_id):
        source = record.source
        if record.linked_manifest_id and record.connected:
            linked = service.store.get("manifest", record.linked_manifest_id, Manifest)
            if linked.source_id != source.id:
                continue
            for entry in linked.entries:
                if len(entries) >= limit:
                    return entries, True
                entries.append(
                    {
                        "path": str(
                            PurePosixPath(".clio-agent/links") / source.id / linked.id / entry.path
                        ),
                        "display_path": f"Linked folders/{source.label}/{entry.path}",
                        "type": "dir" if entry.kind == "directory" else "file",
                        "internal": False,
                        "source_id": source.id,
                        "size": entry.size,
                        "media_type": detect_media_type(entry.path, b"")[0],
                    }
                )
        if (
            not record.download_read_only
            or not source.local_path
            or not record.manifest_id
            or source.materialization not in {"ready", "stale", "transferring"}
        ):
            continue
        manifest = service.store.get("manifest", record.manifest_id, Manifest)
        root = service.store.root / source.id / manifest.id
        if manifest.source_id != source.id or Path(source.local_path).resolve() != root.resolve():
            continue
        duplicate_label = (
            sum(row.source.label == source.label for row in service.sources(workspace_id)) > 1
        )
        display_name = f"{source.label} ({source.id[-8:]})" if duplicate_label else source.label
        for entry in manifest.entries:
            if len(entries) >= limit:
                return entries, True
            virtual = str(_PREFIX / source.id / manifest.id / entry.path)
            try:
                target = safe_child(root, entry.path)
                if not target.exists():
                    continue
                row: dict[str, Any] = {
                    "path": virtual,
                    "display_path": f"Connected data/{display_name}/{entry.path}",
                    "type": "dir" if entry.kind == "directory" else "file",
                    "internal": False,
                    "source_id": source.id,
                }
                if entry.kind == "file":
                    row.update(
                        size=target.stat().st_size, media_type=workspace_file_media_type(target)
                    )
                entries.append(row)
            except OSError:
                continue
    return entries, False


def resolve_connected_input(service: StorageService, workspace_id: str, path: str) -> Path | None:
    """Resolve only the current approved snapshot in the owning workspace."""
    virtual = PurePosixPath(path)
    link_prefix = PurePosixPath(".clio-agent/links")
    if virtual.is_relative_to(link_prefix):
        if ".." in virtual.parts or "\\" in path:
            raise ValueError("Invalid linked source path")
        parts = virtual.relative_to(link_prefix).parts
        if len(parts) < 3:
            raise ValueError("Select a linked file")
        source_id, revision, *relative = parts
        record = service.get(workspace_id, source_id)
        if record.linked_manifest_id != revision:
            raise ValueError("This linked folder changed; refresh Files")
        from clio_agent.gact.storage.linked import linked_file

        return linked_file(service, record, PurePosixPath(*relative).as_posix())
    if not virtual.is_relative_to(_PREFIX):
        return None
    if ".." in virtual.parts or "\\" in path:
        raise ValueError("Invalid connected source path")
    parts = virtual.relative_to(_PREFIX).parts
    if len(parts) < 3:
        raise ValueError("Select a file in a connected source")
    source_id, revision, *relative = parts
    record = service.get(workspace_id, source_id, connected=False)
    if (
        not record.download_read_only
        or record.manifest_id != revision
        or not record.source.local_path
        or record.source.materialization not in {"ready", "stale", "transferring"}
    ):
        raise ValueError("This source snapshot is no longer available")
    manifest = service.store.get("manifest", revision, Manifest)
    selected = PurePosixPath(*relative).as_posix()
    if manifest.source_id != source_id or not any(row.path == selected for row in manifest.entries):
        raise ValueError("The file is not in this source's approved snapshot")
    root = service.store.root / source_id / revision
    if Path(record.source.local_path).resolve() != root.resolve():
        raise ValueError("The source snapshot path does not match its ownership record")
    return safe_child(root, selected)
