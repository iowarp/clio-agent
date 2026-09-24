"""Non-blocking workspace file enumeration for browser and ``@`` surfaces."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from clio_agent.gact.resource_materialization import MANAGED_INPUT_DIRECTORY
from clio_agent.gact.resource_mime import detect_media_type
from clio_agent.gact.routes.workspace_file_policy import skip_workspace_file_directory
from clio_agent.runtime import trace


def workspace_file_media_type(path: Path) -> str:
    """Detect one file from a bounded prefix using CLIO's pinned MIME table."""

    with path.open("rb") as stream:
        head = stream.read(8192)
    return detect_media_type(path.name, head)[0]


async def collect_workspace_file_entries(
    app: FastAPI,
    workspace_id: str,
    root: Path,
    *,
    limit: int,
    include_hidden: bool = True,
) -> list[dict[str, Any]]:
    """Walk ``root`` off-loop and project managed inputs as visible Sources.

    ``.clio`` (agent state, sessions, uploaded-input copies) is walked like any other
    directory when ``include_hidden`` is true (the default, per owner ruling: there is
    no reason to hide it from the Files view). The one exception is
    ``MANAGED_INPUT_DIRECTORY`` (``.clio/inputs``): its contents are surfaced below as
    friendly ``Sources/<resource_id>/<name>`` entries, so the raw subtree is skipped here
    to avoid showing the same uploaded file twice under two different paths.
    When ``include_hidden`` is false, any dotfile or dot-directory (not just ``.clio``)
    is skipped, powering the client's "Hide dot files and folders" setting.
    """

    allow_symlinks = False
    try:
        from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

        policy = FileAccessPolicy.from_mapping(os.environ)
        allow_symlinks = policy.allow_symlinks
    except Exception as exc:  # noqa: BLE001 - failure recorded via trace.event
        trace.event(
            "WORKSPACE",
            "file policy unavailable for %s (%s); symlinks stay excluded",
            workspace_id,
            exc,
        )

    entries: list[dict[str, Any]] = []
    remaining = limit

    def walk(directory: Path) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except (OSError, PermissionError):
            return
        for child in children:
            if remaining <= 0:
                return
            name = child.name
            relative_path = child.relative_to(root)
            relative = str(relative_path)
            if skip_workspace_file_directory(name):
                continue
            if relative_path == MANAGED_INPUT_DIRECTORY:
                # Surfaced separately below as Sources/<resource_id>/<name>.
                continue
            if not include_hidden and name.startswith("."):
                continue
            try:
                if child.is_symlink() and not allow_symlinks:
                    continue
                is_directory = child.is_dir()
            except OSError:
                continue
            entry: dict[str, Any] = {
                "path": relative,
                "type": "dir" if is_directory else "file",
                "internal": False,
            }
            if not is_directory:
                try:
                    stat = child.stat()
                    entry["size"] = stat.st_size
                    entry["media_type"] = workspace_file_media_type(child)
                    entry["modified"] = (
                        datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                except OSError:
                    pass
            entries.append(entry)
            remaining -= 1
            if is_directory:
                walk(child)

    await asyncio.to_thread(walk, root)

    store = getattr(app.state, "resource_store", None)
    if store is None:
        return entries
    resolved_root = root.resolve(strict=False)
    for record in store.list(workspace_id):
        if record.state != "ready" or not record.workspace_path:
            continue
        source_path = Path(record.workspace_path).expanduser().resolve(strict=False)
        try:
            source_relative = source_path.relative_to(resolved_root)
            stat = source_path.stat()
        except (OSError, ValueError):
            continue
        entries.append(
            {
                "path": source_relative.as_posix(),
                "display_path": f"Sources/{record.id}/{record.name}",
                "type": "file",
                "internal": False,
                "source": "managed_input",
                "resource_id": record.id,
                "size": stat.st_size,
                "media_type": record.detected_mime or "application/octet-stream",
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
    return entries


__all__ = ["collect_workspace_file_entries", "workspace_file_media_type"]
