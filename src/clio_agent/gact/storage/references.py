"""Attach approved source bytes through the existing immutable resource custody pipeline."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from clio_agent.gact.resource_custody import ResourceRecord, ResourceStore
from clio_agent.gact.storage.adapters import file_hash, safe_child
from clio_agent.gact.storage.models import FileEntry, Manifest, SourceRecord
from clio_agent.gact.storage.store import SourceStore
from clio_agent.platform_paths import win_extended_path


def folder_entries(manifest: Manifest, folder: str = "") -> list[FileEntry]:
    """Select an approved directory and its descendants, keeping source-relative paths."""
    if not folder:
        return manifest.entries
    FileEntry(path=folder, kind="directory")
    children = [entry for entry in manifest.entries if entry.path.startswith(folder + "/")]
    if not children and not any(
        entry.path == folder and entry.kind == "directory" for entry in manifest.entries
    ):
        raise ValueError("Choose a folder in this source")
    return children


def source_folder_resource(
    sources: SourceStore,
    resources: ResourceStore,
    record: SourceRecord,
    *,
    linked: bool = False,
    folder: str = "",
) -> ResourceRecord:
    """Attach an immutable folder index through the ordinary resource reference pipeline.

    Only approved metadata enters the index. File bytes remain on the existing
    fsspec or downloaded-input path and are read individually by connected_data_open.
    """
    source = record.source
    if linked:
        if not record.connected or not record.linked_manifest_id:
            raise ValueError("Link this folder before attaching it")
        revision = record.linked_manifest_id
    else:
        if (
            not source.local_path
            or source.materialization not in {"ready", "stale", "transferring"}
            or not record.manifest_id
        ):
            raise ValueError("Download this folder before attaching it")
        revision = record.manifest_id
    manifest = sources.get("manifest", revision, Manifest)
    if manifest.source_id != source.id:
        raise ValueError("Folder index ownership mismatch")
    entries = folder_entries(manifest, folder)
    label = f"{source.label} / {folder}" if folder else source.label
    body = json.dumps(
        {
            "source_id": source.id,
            "label": label,
            "folder": folder,
            "provider": source.provider,
            "revision": revision,
            "linked": linked,
            "access": record.linked_access
            if linked
            else ("read_only" if record.download_read_only else "editable"),
            "writes_propagate": linked and record.linked_access == "write_through",
            "entry_count": len(entries),
            "entries": [entry.model_dump(exclude={"locator"}) for entry in entries[:200]],
            "next_offset": 200 if len(entries) > 200 else None,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    resource, _ = resources.create_or_resume(
        workspace_id=source.workspace_id or "",
        name="folder-index.json",
        declared_size=len(body),
        client_upload_id="source-folder:" + digest,
    )
    if resource.state == "uploading":
        resource = resources.append(
            resource.id, offset=resource.received_size, data=body[resource.received_size :]
        )
    if resource.state != "ready" or resource.sha256 != digest:
        raise ValueError("The folder reference could not be prepared; attach it again")
    return resources.set_connected_source(
        resource.id,
        {
            "id": source.id,
            "kind": "folder",
            "provider": source.provider,
            "label": label,
            "folder": folder,
            "revision": revision,
            "linked": str(linked).lower(),
        },
    )


def source_resource(
    sources: SourceStore,
    resources: ResourceStore,
    record: SourceRecord,
    relative: str,
    *,
    linked_path: Path | None = None,
) -> ResourceRecord:
    """Preserve source identity and exact bytes before a reference enters a conversation.

    Read-only and working-copy references attach the approved immutable input.
    Live writable sources are snapshotted at the user's explicit attach action.
    No provider request or secret is needed on the model's resource read path.
    """
    source = record.source
    expected: str | None
    if linked_path is not None:
        if not record.linked_manifest_id:
            raise ValueError("This source has no approved linked folder")
        root = sources.root / source.id / (record.linked_manifest_id or "")
        if safe_child(root, relative).resolve() != linked_path.resolve():
            raise ValueError("Linked read ownership mismatch")
        expected = file_hash(linked_path)
    elif not source.local_path or source.materialization not in {"ready", "stale", "transferring"}:
        raise ValueError("Transfer this source before attaching its files")
    elif not record.download_read_only:
        if not record.connected:
            raise ValueError("Reconnect the live source before attaching a file")
        root = Path(source.local_path)
        expected = file_hash(safe_child(root, relative))
    else:
        if not record.manifest_id:
            raise ValueError("This source has no approved baseline")
        manifest = sources.get("manifest", record.manifest_id, Manifest)
        expected = manifest.hashes.get(relative)
        if expected is None:
            raise ValueError("Select a file in the approved source snapshot")
        root = sources.root / source.id / record.manifest_id
    path = safe_child(root, relative)
    if not os.path.isfile(win_extended_path(path)):
        raise ValueError("Select a regular file")
    origin = {
        "id": source.id,
        "provider": source.provider,
        "label": source.label,
        "clio_id": source.owner.clio_id,
        "host_id": source.owner.host_id,
        "root": source.root,
        "path": relative,
        "revision": (record.linked_manifest_id or expected)
        if linked_path
        else source.revision or expected,
        "sha256": expected,
        "mode": source.mode,
    }
    identity = hashlib.sha256(
        f"{source.id}\0{source.revision}\0{relative}\0{expected}".encode()
    ).hexdigest()
    resource, _ = resources.create_or_resume(
        workspace_id=source.workspace_id or "",
        name=path.name,
        declared_size=os.stat(win_extended_path(path)).st_size,
        client_upload_id="source:" + identity,
    )
    if resource.state == "uploading":
        with open(win_extended_path(path), "rb") as stream:
            stream.seek(resource.received_size)
            while chunk := stream.read(1024 * 1024):
                resource = resources.append(resource.id, offset=resource.received_size, data=chunk)
    if resource.state != "ready" or resource.sha256 != expected:
        resources.delete(resource.workspace_id, resource.id)
        raise ValueError("The source file changed while attaching it; refresh and select it again")
    return resources.set_connected_source(resource.id, origin)
