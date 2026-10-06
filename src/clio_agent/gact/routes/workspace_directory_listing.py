"""Page immediate directory children without recursively opening workspace files."""

from __future__ import annotations

import asyncio
import heapq
import os
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from fastapi import FastAPI, HTTPException

from clio_agent.gact.routes.workspace_file_policy import (
    is_internal_workspace_file_directory,
    skip_workspace_file_directory,
    workspace_read_redaction_reason,
)
from clio_agent.gact.storage.models import Manifest

DIRECTORY_PAGE_SIZE = 200
_GROUPS = {
    ".clio-agent/sources": "Connected data",
    ".clio-agent/links": "Linked folders",
    ".clio-agent/inputs": "Uploads",
}


def _directory(path: str, label: str | None = None) -> dict[str, Any]:
    return {"path": path, "display_path": label or path, "type": "dir", "internal": False}


def _sources(app: FastAPI, workspace_id: str, directory: str) -> list[dict[str, Any]]:
    """Project only the requested level of approved source manifests."""
    service = getattr(app.state, "connected_storage", None)
    if service is None:
        return []
    entries: dict[str, dict[str, Any]] = {}
    for record in service.sources(workspace_id):
        source = record.source
        linked = directory.startswith(".clio-agent/links")
        revision = record.linked_manifest_id if linked else record.manifest_id
        available = record.connected if linked else record.download_read_only
        if not available or not revision:
            continue
        if not linked and (
            not source.local_path
            or source.materialization not in {"ready", "stale", "transferring"}
        ):
            continue
        if (
            not linked
            and Path(source.local_path).resolve()
            != (service.store.root / source.id / revision).resolve()
        ):
            continue
        manifest = service.store.get("manifest", revision, Manifest)
        if manifest.source_id != source.id:
            continue
        group = ".clio-agent/links" if linked else ".clio-agent/sources"
        prefix = f"{group}/{source.id}/{revision}"
        if directory == group:
            entries[prefix] = _directory(prefix, f"{source.label} ({source.id[-8:]})")
            continue
        if directory != prefix and not directory.startswith(prefix + "/"):
            continue
        relative = directory[len(prefix) :].strip("/")
        for item in manifest.entries:
            item_path = PurePosixPath(item.path).as_posix()
            if ".." in PurePosixPath(item_path).parts or "\\" in item.path:
                continue
            if relative and not item_path.startswith(relative + "/"):
                continue
            tail = item_path[len(relative) :].strip("/")
            if not tail:
                continue
            name = tail.split("/")[0]
            path = f"{directory}/{name}"
            if "/" in tail or item.kind == "directory":
                entries[path] = _directory(path, name)
            elif path not in entries:
                entries[path] = {
                    "path": path,
                    "display_path": name,
                    "type": "file",
                    "internal": False,
                    "size": item.size,
                    "source_id": source.id,
                }
    return list(entries.values())


def _uploads(app: FastAPI, workspace_id: str, root: Path) -> list[dict[str, Any]]:
    """Use recorded upload ownership, preserving each file's existing read path."""
    from clio_agent.paths import workspace_state_dir

    store = getattr(app.state, "resource_store", None)
    if store is None:
        return []
    entries = []
    for record in store.list(workspace_id):
        if record.state != "ready" or not record.workspace_path:
            continue
        source = Path(record.workspace_path).resolve()
        try:
            path = source.relative_to(root).as_posix()
        except ValueError:
            try:
                path = (
                    ".clio-agent/local/"
                    + source.relative_to(workspace_state_dir(root).resolve()).as_posix()
                )
            except ValueError:
                continue
        entries.append(
            {
                "path": path,
                "display_path": f"{record.name} ({record.id[-8:]})",
                "type": "file",
                "internal": False,
                "source": "managed_input",
                "resource_id": record.id,
                "media_type": record.detected_mime,
                "size": record.received_size,
            }
        )
    return entries


def _local_children(
    root: Path, directory: str, *, include_hidden: bool, exclude_service_storage: bool
) -> Iterable[dict[str, Any]]:
    """Enumerate one level with the same directory and symlink policy as the picker."""
    from clio_agent.tools.file_policy import FileAccessPolicy

    requested = Path(directory)
    if requested.is_absolute() or ".." in requested.parts:
        raise HTTPException(status_code=400, detail="Invalid workspace directory")
    target = root / requested
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=403, detail="Directory is outside the workspace")
    if workspace_read_redaction_reason(requested) == "sandbox_child_cache":
        return
    if any(skip_workspace_file_directory(part) for part in requested.parts):
        return
    if not include_hidden and any(part.startswith(".") for part in requested.parts):
        return
    if exclude_service_storage and any(
        is_internal_workspace_file_directory(p) for p in requested.parts
    ):
        return
    allow_symlinks = FileAccessPolicy.from_mapping(os.environ).allow_symlinks
    if not allow_symlinks and any(
        (root / Path(*requested.parts[:index])).is_symlink()
        for index in range(1, len(requested.parts) + 1)
    ):
        raise HTTPException(status_code=403, detail="Symlink directories are excluded")
    try:
        with os.scandir(target) as children:
            for child in children:
                if skip_workspace_file_directory(child.name):
                    continue
                if not include_hidden and child.name.startswith("."):
                    continue
                if exclude_service_storage and is_internal_workspace_file_directory(child.name):
                    continue
                relative = (requested / child.name).as_posix()
                if relative == ".clio/inputs" or relative in _GROUPS:
                    continue
                try:
                    if child.is_symlink() and not allow_symlinks:
                        continue
                    is_directory = child.is_dir()
                    row: dict[str, Any] = {
                        "path": relative,
                        "type": "dir" if is_directory else "file",
                        "internal": False,
                    }
                    if is_directory and workspace_read_redaction_reason(Path(relative)):
                        row["redacted"] = "sandbox_child_cache"
                    yield row
                except OSError:
                    continue
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Directory no longer exists") from exc
    except (NotADirectoryError, PermissionError) as exc:
        raise HTTPException(status_code=403, detail="Directory cannot be listed") from exc


async def collect_workspace_directory(
    app: FastAPI,
    workspace_id: str,
    root: Path,
    *,
    directory: str,
    offset: int,
    include_hidden: bool,
    exclude_service_storage: bool,
) -> dict[str, Any]:
    """Return one deterministic page; closed descendants and file bodies are never read."""
    directory = directory.replace("\\", "/")
    if (
        ".." in PurePosixPath(directory).parts
        or Path(directory).is_absolute()
        or directory.startswith("/")
    ):
        raise HTTPException(status_code=400, detail="Invalid workspace directory")
    directory = directory.strip("/")
    root = root.resolve()

    def collect() -> dict[str, Any]:
        if directory == ".clio-agent/inputs":
            rows: Iterable[dict[str, Any]] = _uploads(app, workspace_id, root)
        elif any(
            directory == group or directory.startswith(group + "/") for group in list(_GROUPS)[:2]
        ):
            rows = _sources(app, workspace_id, directory)
        else:
            rows = _local_children(
                root,
                directory,
                include_hidden=include_hidden,
                exclude_service_storage=exclude_service_storage,
            )
            if not directory:
                from itertools import chain

                groups = [
                    _directory(path, label)
                    for path, label in _GROUPS.items()
                    if (
                        _uploads(app, workspace_id, root)
                        if path.endswith("inputs")
                        else _sources(app, workspace_id, path)
                    )
                ]
                rows = chain(rows, groups)
        page = heapq.nsmallest(
            offset + DIRECTORY_PAGE_SIZE + 1,
            rows,
            key=lambda row: (
                row["type"] != "dir",
                (row.get("display_path") or row["path"]).casefold(),
                row["path"],
            ),
        )
        more = len(page) > offset + DIRECTORY_PAGE_SIZE
        return {
            "entries": page[offset : offset + DIRECTORY_PAGE_SIZE],
            "truncated": more,
            "next_offset": offset + DIRECTORY_PAGE_SIZE if more else None,
        }

    return await asyncio.to_thread(collect)
