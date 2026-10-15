"""Stable Files-view locators for the existing live folder-grant projection."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from clio_agent.runtime.sandbox_roots import granted_write_roots

FOLDER_PREFIX = ".clio-agent/folders"


def folder_locations(workspace_root: str | Path) -> dict[str, Path]:
    """Map stable virtual folder locators to explicitly granted real folders."""
    return {
        f"{FOLDER_PREFIX}/{hashlib.sha256(str(root).encode()).hexdigest()[:20]}": root
        for root in granted_write_roots(str(workspace_root))
    }


def resolve_folder_location(workspace_root: str | Path, path: str | Path) -> Path | None:
    """Resolve a granted-folder locator; reject revoked, traversal and symlink escapes."""
    raw = Path(path)
    if raw.is_absolute():
        try:
            raw = raw.relative_to(Path(workspace_root))
        except ValueError:
            if any(
                raw.resolve().is_relative_to(root)
                for root in folder_locations(workspace_root).values()
            ):
                return raw
            return None
    locator = raw.as_posix()
    if not locator.startswith(FOLDER_PREFIX + "/"):
        return None
    parts = PurePosixPath(locator).parts
    if ".." in parts:
        raise PermissionError("Folder path cannot traverse outside its permitted root")
    prefix = "/".join(parts[:3])
    root = folder_locations(workspace_root).get(prefix)
    if root is None:
        raise PermissionError("Folder access is no longer permitted")
    target = root.joinpath(*parts[3:])
    if not target.resolve().is_relative_to(root.resolve()):
        raise PermissionError("Folder path escapes its permitted root")
    return target
