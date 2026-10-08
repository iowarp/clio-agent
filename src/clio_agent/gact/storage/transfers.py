"""Explicit source materialization with durable progress and immutable baselines."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from clio_agent.gact.storage.adapters import SourceAdapter, file_hash, safe_child
from clio_agent.gact.storage.filesystem import is_link, walk
from clio_agent.gact.storage.models import FileEntry, Manifest, SourceRecord, TransferOperation
from clio_agent.gact.storage.store import SourceStore
from clio_agent.platform_paths import (
    atomic_replace,
    copytree_extended,
    rmtree_extended,
    win_extended_path,
)


class TransferCancelled(RuntimeError):
    """A user cancelled this operation; completed baselines remain intact."""


def check_cancel(store: SourceStore, operation_id: str) -> None:
    """Observe durable cancellation between reads and file operations."""
    if store.get("operation", operation_id, TransferOperation).cancel_requested:
        raise TransferCancelled("Transfer cancelled")


def snapshot_revision(entries: list[FileEntry]) -> str:
    """Identify the exact ordered provider revision snapshot."""
    payload = json.dumps([entry.model_dump() for entry in entries], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def make_read_only(root: Path) -> None:
    """Remove filesystem write bits from an owned baseline and its contents."""
    for directory, _, files in walk(root, topdown=False):
        for name in files:
            os.chmod(win_extended_path(directory / name), 0o444)
        os.chmod(win_extended_path(directory), 0o555)


def remove_owned_tree(path: Path, owner_root: Path) -> None:
    """Remove only a checked descendant of a namespaced ownership root."""
    path.resolve().relative_to(owner_root.resolve())
    if path.resolve() == owner_root.resolve() or is_link(path):
        raise ValueError("Refusing to remove a storage ownership root or symlink")
    if not os.path.exists(win_extended_path(path)):
        return
    for directory, names, files in walk(path):
        for name in names:
            child = Path(directory) / name
            if is_link(child):
                raise ValueError("Refusing cleanup through a linked directory")
        os.chmod(win_extended_path(directory), 0o700)
        for name in files:
            child = Path(directory) / name
            if not is_link(child):
                os.chmod(win_extended_path(child), 0o600)
    rmtree_extended(path)


def materialize(
    store: SourceStore,
    record: SourceRecord,
    adapter: SourceAdapter,
    workspace_root: Path,
    operation: TransferOperation,
) -> SourceRecord:
    """Stream selected data on the connected CLIO and publish only a complete copy.

    A retry creates a new immutable revision. Failed and cancelled staging trees
    are removed; previous manifests and workspace copies remain available.
    """
    source = record.source
    if operation.selected_paths is not None:
        from clio_agent.gact.storage.downloads import SelectedDownload

        adapter = SelectedDownload(store, record, adapter, operation.selected_paths)
    owner_root = store.root / source.id
    os.makedirs(win_extended_path(owner_root), exist_ok=True)
    stage = owner_root / ("stage-" + operation.id)
    baseline_id = "snapshot_" + uuid.uuid4().hex
    baseline = owner_root / baseline_id
    previous_path = Path(source.local_path) if source.local_path else None
    workspace_stage: Path | None = None
    store.update_operation(operation.id, state="running")
    try:
        entries = adapter.entries()
        check_cancel(store, operation.id)
        revision = snapshot_revision(entries)
        total = sum(row.size for row in entries if row.kind == "file")
        store.update_operation(operation.id, bytes_total=total)
        required = total * (2 if not record.download_read_only else 1)
        if shutil.disk_usage(owner_root).free < required:
            raise ValueError("Insufficient capacity for the source baseline and working copy")
        if not record.download_read_only and shutil.disk_usage(workspace_root).free < total:
            raise ValueError("Insufficient workspace capacity for the working copy")
        os.mkdir(win_extended_path(stage))
        done = 0
        hashes: dict[str, str] = {}
        seen: set[str] = set()
        for entry in entries:
            check_cancel(store, operation.id)
            collision_key = entry.path.casefold()
            if collision_key in seen:
                raise ValueError(
                    "The source contains conflicting filenames; select a smaller folder"
                )
            seen.add(collision_key)
            destination = safe_child(stage, entry.path)
            if entry.kind == "directory":
                os.makedirs(win_extended_path(destination), exist_ok=True)
                continue
            os.makedirs(win_extended_path(destination.parent), exist_ok=True)
            digest = hashlib.sha256()
            file_bytes = 0
            with (
                adapter.open_read(entry) as reader,
                open(win_extended_path(destination), "xb") as writer,
            ):
                while chunk := reader.read(1024 * 1024):
                    check_cancel(store, operation.id)
                    file_bytes += len(chunk)
                    if source.provider == "globus" and file_bytes > entry.size:
                        raise ValueError(
                            "Source file size changed during transfer; retry the transfer"
                        )
                    writer.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    store.update_operation(operation.id, bytes_done=done)
                writer.flush()
                os.fsync(writer.fileno())
            if source.provider == "globus" and file_bytes != entry.size:
                raise ValueError("Source file was truncated during transfer; retry the transfer")
            hashes[entry.path] = digest.hexdigest()
            if entry.sha256 and hashes[entry.path] != entry.sha256:
                raise ValueError("Source bytes changed during transfer; refresh to try again")
        if snapshot_revision(adapter.entries()) != revision:
            raise ValueError(
                "The source changed during transfer; refresh to obtain a consistent copy"
            )
        check_cancel(store, operation.id)
        atomic_replace(stage, baseline)
        manifest = Manifest(
            id=baseline_id, source_id=source.id, revision=revision, entries=entries, hashes=hashes
        )
        store.put("manifest", baseline_id, manifest)
        if not record.download_read_only:
            # Stable opaque directory names avoid label renames moving users' files.
            destination = workspace_root / "connected-data" / source.id
            if destination.exists():
                old = store.get("manifest", record.manifest_id or "", Manifest)
                if local_hashes(destination) != old.hashes:
                    raise ValueError(
                        "The working copy has local changes; review them before refreshing"
                    )
            os.makedirs(win_extended_path(destination.parent), exist_ok=True)
            if destination.parent.is_symlink():
                raise ValueError("The connected-data folder must not be a symbolic link")
            workspace_stage = destination.with_name("stage-" + operation.id)
            copytree_extended(baseline, workspace_stage)
            check_cancel(store, operation.id)
            if destination.exists():
                # Retain the old copy in the same filesystem; never imply an atomic
                # multi-directory replacement or discard edits after a failed rename.
                archived = destination.with_name("previous-" + operation.id)
                atomic_replace(destination, archived)
                try:
                    atomic_replace(workspace_stage, destination)
                except OSError:
                    atomic_replace(archived, destination)
                    raise
            else:
                atomic_replace(workspace_stage, destination)
        else:
            destination = baseline
        make_read_only(baseline)
        record.manifest_id = baseline_id
        record.source = source.model_copy(
            update={
                "local_path": str(destination),
                "revision": revision,
                "materialization": "ready",
                "operation_id": operation.id,
            }
        )
        store.put("source", source.id, record)
        store.update_operation(operation.id, state="completed", bytes_done=done)
        return record
    except (OSError, ValueError, RuntimeError) as exc:
        state = "cancelled" if isinstance(exc, TransferCancelled) else "failed"
        store.update_operation(operation.id, state=state, error=str(exc))
        record.source = source.model_copy(
            update={
                "materialization": "ready"
                if previous_path and previous_path.exists()
                else "failed",
                "operation_id": operation.id,
            }
        )
        store.put("source", source.id, record)
        raise
    finally:
        remove_owned_tree(stage, owner_root)
        if workspace_stage is not None:
            remove_owned_tree(workspace_stage, workspace_root / "connected-data")


def local_hashes(root: Path) -> dict[str, str]:
    """Hash a working tree without following links or accepting special files."""
    hashes: dict[str, str] = {}
    for directory, names, files in walk(root):
        for name in names + files:
            path = safe_child(root, (Path(directory) / name).relative_to(root).as_posix())
            if os.path.isfile(win_extended_path(path)):
                hashes[path.relative_to(root).as_posix()] = file_hash(path)
            elif not os.path.isdir(win_extended_path(path)):
                raise ValueError("Working copies may contain only regular files and directories")
    return hashes
