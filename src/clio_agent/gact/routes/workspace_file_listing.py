"""Non-blocking workspace file enumeration for browser and ``@`` surfaces."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from clio_agent.gact.resource_materialization import MANAGED_INPUT_DIRECTORY
from clio_agent.gact.resource_mime import detect_media_type
from clio_agent.gact.routes.workspace_file_policy import (
    is_internal_workspace_file_directory,
    skip_workspace_file_directory,
)
from clio_agent.runtime import trace
from clio_agent.runtime.sandbox import CHILD_CACHE_DIRNAME


def workspace_file_media_type(path: Path) -> str:
    """Detect one file from a bounded prefix using CLIO's pinned MIME table."""

    with path.open("rb") as stream:
        head = stream.read(8192)
    return detect_media_type(path.name, head)[0]


@dataclass(frozen=True)
class WorkspaceFileWalk:
    """A capped directory walk's entries plus an HONEST truncation signal.

    ``truncated`` is computed from the real walk, not inferred after the fact from a
    filtered subset of ``entries`` — a caller that drops entries (e.g. the repo-map
    endpoint excluding service storage) must not derive truncation from what is left
    over, or a walk that filled its cap entirely on excluded content reports
    ``truncated=False`` while silently showing an incomplete tree (#S3 review).
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


async def collect_workspace_file_entries(
    app: FastAPI,
    workspace_id: str,
    root: Path,
    *,
    limit: int,
    include_hidden: bool = True,
    exclude_service_storage: bool = False,
) -> WorkspaceFileWalk:
    """Walk ``root`` off-loop and project managed inputs as visible Sources.

    ``.clio`` (agent state, sessions, uploaded-input copies) is walked like any other
    directory when ``include_hidden`` is true (the default, per owner ruling: there is
    no reason to hide it from the Files view) and ``exclude_service_storage`` is false.
    Two exceptions:

    - ``MANAGED_INPUT_DIRECTORY`` (``.clio/inputs``): its contents are surfaced below as
      friendly ``Sources/<resource_id>/<name>`` entries, so the raw subtree is skipped
      here to avoid showing the same uploaded file twice under two different paths.
    - ``CHILD_CACHE_DIRNAME`` (``.clio-child-cache``): the redirected APPDATA/TEMP/
      XDG_CACHE_HOME home for sandboxed MCP child processes, which can hold package- or
      provider-credential caches (security review). The folder itself is listed — the
      owner ruling is to show dot folders, and hiding its EXISTENCE would be a silent
      omission — but its contents are never walked; the entry instead carries a typed
      ``redacted`` reason. ``GET /files/read`` separately refuses to serve raw bytes
      from under it (see ``workspace_file_policy.workspace_read_redaction_reason``).

    ``include_hidden=False`` skips every dotfile/dot-directory generically (the
    client's "Hide dot files and folders" toggle). ``exclude_service_storage=True``
    instead skips only CLIO's own ``.clio``/``.clio-*`` service storage UP FRONT,
    during the walk itself — never as a post-walk filter, which would burn the cap on
    excluded content and then report an honest-looking ``truncated=False`` while
    silently truncating the user's own files (the repo-map endpoint's prior, narrower
    scope; unrelated ordinary dotfiles like ``.github`` still walk normally).

    At every level, non-dot entries are walked before dot entries so a huge ``.clio``
    (transcripts, ARC state, artifacts) can never starve the cap before the user's own
    files are reached.
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
    truncated = False

    def walk(directory: Path) -> None:
        nonlocal remaining, truncated
        try:
            # Non-dot entries sort first (`False < True`), so a directory's own
            # files/subfolders are always walked to completion before any of its
            # dot-prefixed siblings spend a single unit of the shared cap.
            children = sorted(
                directory.iterdir(),
                key=lambda path: (path.name.startswith("."), path.name),
            )
        except (OSError, PermissionError):
            return
        for child in children:
            if remaining <= 0:
                truncated = True
                return
            name = child.name
            relative_path = child.relative_to(root)
            relative = str(relative_path)
            if skip_workspace_file_directory(name):
                continue
            if relative_path == MANAGED_INPUT_DIRECTORY:
                # Surfaced separately below as Sources/<resource_id>/<name>.
                continue
            if exclude_service_storage and is_internal_workspace_file_directory(name):
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
            redacted_contents = is_directory and name == CHILD_CACHE_DIRNAME
            if redacted_contents:
                entry["redacted"] = "sandbox_child_cache"
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
            if is_directory and not redacted_contents:
                walk(child)

    await asyncio.to_thread(walk, root)

    store = getattr(app.state, "resource_store", None)
    if store is None:
        return WorkspaceFileWalk(entries=entries, truncated=truncated)
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
    return WorkspaceFileWalk(entries=entries, truncated=truncated)


__all__ = ["WorkspaceFileWalk", "collect_workspace_file_entries", "workspace_file_media_type"]
