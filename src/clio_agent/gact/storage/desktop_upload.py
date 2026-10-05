"""Publish an explicitly uploaded desktop folder without flattening its file hierarchy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Callable

from clio_schemas.connected_resources import ConnectedSource, ResourceOwner, SourceCapabilities
from pydantic import Field, field_validator

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.adapters import file_hash, safe_child
from clio_agent.gact.storage.models import FileEntry, Manifest, SourceRecord, StorageModel
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.transfers import make_read_only, remove_owned_tree, snapshot_revision
from clio_agent.platform_paths import rename_extended, win_extended_path


class UploadedSourceFile(StorageModel):
    """One custody original and its desktop-relative path."""

    path: str
    resource_id: str
    revision: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        """Use the same cross-platform path safety as every source adapter."""
        return FileEntry(path=value, kind="file").path


class UploadedSource(StorageModel):
    """A complete folder manifest; uploads remain resumable in ordinary resource custody."""

    label: str = Field(min_length=1, max_length=120)
    files: list[UploadedSourceFile] = Field(min_length=1, max_length=10000)
    draft: bool = False


def publish_uploaded_source(
    store: SourceStore,
    resources: ResourceStore,
    workspace_id: str,
    workspace_root: Path,
    principal: str,
    request: UploadedSource,
    on_prepare: Callable[[SourceRecord], None] | None = None,
) -> SourceRecord:
    """Validate the complete revision, then publish a protected namespaced folder.

    Repeating the same manifest returns the same source. It does not grant the
    connected CLIO write access to the desktop or silently synchronize it.
    """
    seen: set[str] = set()
    originals = []
    for item in sorted(request.files, key=lambda row: row.path):
        if item.path.casefold() in seen:
            raise ValueError("Desktop folder contains colliding filenames")
        seen.add(item.path.casefold())
        uploaded = resources.get(workspace_id, item.resource_id)
        if uploaded is None or uploaded.revision != item.revision or uploaded.state != "ready":
            raise ValueError("Every folder file must be fully uploaded to this CLIO workspace")
        originals.append((item, uploaded))
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "workspace": workspace_id,
                "principal": principal,
                "label": request.label,
                "files": [
                    (item.path, record.id, record.revision, record.sha256)
                    for item, record in originals
                ],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    identifier = "source_desktop_" + fingerprint[:32]
    try:
        existing = store.get("source", identifier, SourceRecord)
    except KeyError:
        existing = None
    if existing and existing.source.materialization == "ready":
        return existing
    owner = store.root / identifier
    owner.mkdir(exist_ok=True)
    baseline = owner / ("snapshot_" + fingerprint[:32])
    record = SourceRecord(
        source=ConnectedSource(
            id=identifier,
            provider="local",
            label=request.label,
            root=str(baseline),
            owner=ResourceOwner(clio_id=store.clio_id, host_id="local"),
            workspace_id=workspace_id,
            capabilities=SourceCapabilities(
                search=True,
                supported_modes=["read_only"],
                unavailable_reasons={
                    "working_copy": "A browser upload cannot apply reviewed changes back to the desktop. Copy inputs into your workspace to edit them.",
                    "write_enabled": "The desktop folder is not mounted on this CLIO.",
                },
            ),
        ),
        principal=principal,
        workspace_root=str(workspace_root.resolve()),
        origin="desktop_upload",
    )
    store.put("source", identifier, record)
    if on_prepare is not None:
        on_prepare(record)
    operation = store.begin_operation(identifier, "materialize")
    stage = owner / ("stage-" + operation.id)
    try:
        total = sum(resource.declared_size for _, resource in originals)
        if shutil.disk_usage(owner).free < total:
            raise ValueError("Insufficient capacity for the uploaded folder baseline")
        stage.mkdir()
        store.update_operation(operation.id, state="running", bytes_total=total)
        done = 0
        entries: dict[str, FileEntry] = {}
        hashes = {}
        for item, resource in originals:
            target = safe_child(stage, item.path)
            os.makedirs(win_extended_path(target.parent), exist_ok=True)
            shutil.copyfile(
                win_extended_path(resources.content_path(resource)), win_extended_path(target)
            )
            if file_hash(target) != resource.sha256:
                raise ValueError("Uploaded file failed its custody hash check")
            entries[item.path] = FileEntry(
                path=item.path,
                kind="file",
                size=resource.declared_size,
                revision=resource.sha256,
                sha256=resource.sha256,
            )
            hashes[item.path] = resource.sha256
            for parent in PurePosixPath(item.path).parents:
                if str(parent) != ".":
                    entries[str(parent)] = FileEntry(path=str(parent), kind="directory")
            done += resource.declared_size
            store.update_operation(operation.id, bytes_done=done)
        rows = sorted(entries.values(), key=lambda entry: entry.path)
        manifest = Manifest(
            id=baseline.name,
            source_id=identifier,
            revision=snapshot_revision(rows),
            entries=rows,
            hashes=hashes,
        )
        if baseline.exists():
            # A prior process may have completed the rename before persisting its
            # receipt. Validate those bytes instead of deleting retained evidence.
            if any(
                file_hash(safe_child(baseline, path)) != digest for path, digest in hashes.items()
            ):
                raise ValueError("An existing upload baseline failed verification")
        else:
            rename_extended(stage, baseline)
        make_read_only(baseline)
        store.put("manifest", manifest.id, manifest)
        record.manifest_id = manifest.id
        record.source = record.source.model_copy(
            update={
                "revision": manifest.revision,
                "materialization": "ready",
                "local_path": str(baseline),
                "operation_id": operation.id,
            }
        )
        store.put("source", identifier, record)
        store.update_operation(operation.id, state="completed", bytes_done=done)
        return record
    except (OSError, ValueError, RuntimeError) as exc:
        store.update_operation(operation.id, state="failed", error=str(exc))
        record.source = record.source.model_copy(
            update={"materialization": "failed", "operation_id": operation.id}
        )
        store.put("source", identifier, record)
        raise
    finally:
        remove_owned_tree(stage, owner)
