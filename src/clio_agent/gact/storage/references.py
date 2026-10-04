"""Attach approved source bytes through the existing immutable resource custody pipeline."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from clio_agent.gact.resource_custody import ResourceRecord, ResourceStore
from clio_agent.gact.storage.adapters import file_hash, safe_child
from clio_agent.gact.storage.models import Manifest, SourceRecord
from clio_agent.gact.storage.store import SourceStore
from clio_agent.platform_paths import win_extended_path


def source_resource(
    sources: SourceStore, resources: ResourceStore, record: SourceRecord, relative: str
) -> ResourceRecord:
    """Preserve source identity and exact bytes before a reference enters a conversation.

    Read-only and working-copy references attach the approved immutable input.
    Live writable sources are snapshotted at the user's explicit attach action.
    No provider request or secret is needed on the model's resource read path.
    """
    source = record.source
    if not source.local_path or source.materialization not in {"ready", "stale", "transferring"}:
        raise ValueError("Transfer this source before attaching its files")
    if source.mode == "write_enabled":
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
        "revision": source.revision or expected,
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
