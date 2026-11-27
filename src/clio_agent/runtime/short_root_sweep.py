"""Remove short roots whose home or target is gone, or that a newer root replaced.

Deep folders get short stand-ins (``session_scratch.short_root`` in the system temp,
``paths._windows_short_root`` under ``%LOCALAPPDATA%\\clio-{m,p,s,u}``). Each names what
it stands in for, so a root is judged by that name alone, never by a guess:

* a link (junction or symlink) whose target no longer exists;
* a folder whose ``owner.txt`` names a path whose parent no longer exists (its home or
  session was deleted) on a drive that is still there;
* a system-temp uv stand-in for a home's ``uv-shared`` cache now served by that home's
  ``clio-u`` root (the stand-in held a full uv cache in the temp folder for the life of
  the home, and nothing removed it).

A name whose hash does not match what it names is not CLIO's and is left alone. Each
removal and failure is traced (``fenced_removal`` removes what fenced children made).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from clio_agent import paths
from clio_agent.runtime import session_scratch, trace

#: Typed reasons, one per rule above.
WHY_HOME_GONE = "short_root_owner_gone"
WHY_TARGET_GONE = "short_root_target_gone"
WHY_SUPERSEDED = "short_root_superseded"
_TEMP_NAME = re.compile(r"^clio-[^-]+-([0-9a-f]{16})$")
_LOCAL_NAME = re.compile(r"^[0-9a-f]{10}$")


def _digest(path: str, size: int) -> str:
    return hashlib.sha256(os.path.normcase(path).encode("utf-8")).hexdigest()[:size]


def _owner(folder: Path) -> str:
    try:
        return (folder / "owner.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _gone(path: Path) -> bool:
    """Whether ``path`` is gone from a drive that is still present (never a missing drive)."""
    return Path(path.anchor).exists() and not os.path.lexists(path)


def _remove(folder: Path, why: str, owner: str) -> bool:
    from clio_agent.runtime.fenced_removal import remove_tree  # noqa: PLC0415

    removed = remove_tree(folder, why=why)
    if removed:
        trace.event("SCRATCH", "%s path=%s owner=%s", why, folder, owner)
    return removed


def _sweep_local(base: Path) -> list[str]:
    removed: list[str] = []
    for tag in paths.WINDOWS_SHORT_ROOT_TAGS:
        root = base / tag
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            owner = _owner(folder) if _LOCAL_NAME.match(folder.name) else ""
            if not owner or _digest(owner, 10) != folder.name:
                continue
            if _gone(Path(owner).parent) and _remove(folder, WHY_HOME_GONE, owner):
                removed.append(str(folder))
    return removed


def _superseded(owner: str) -> bool:
    if os.name != "nt" or Path(owner).name != "uv-shared":
        return False
    local = os.environ.get("LOCALAPPDATA", "").strip()
    replacement = Path(local) / "clio-u" / _digest(owner, 10) if local else None
    return replacement is not None and session_scratch.same_path(_owner(replacement), Path(owner))


def _sweep_temp(base: Path) -> list[str]:
    removed: list[str] = []
    for entry in sorted(base.glob("clio-*")):
        match = _TEMP_NAME.match(entry.name)
        try:
            info = entry.lstat()
        except OSError:
            continue
        if match is None or not session_scratch.is_mine(info):
            continue
        if stat.S_ISLNK(info.st_mode) or entry.is_junction():
            try:
                target = os.readlink(entry).removeprefix("\\\\?\\").removeprefix("\\??\\")
            except OSError:
                continue
            if _digest(target.rstrip("\\/"), 16) != match.group(1) or not _gone(Path(target)):
                continue
            try:
                if entry.is_junction():
                    entry.rmdir()  # removes the junction, never its target
                else:
                    entry.unlink()
            except OSError as exc:
                trace.event("SCRATCH", "short_root_remove_failed path=%s error=%r", entry, exc)
                continue
            trace.event("SCRATCH", "%s path=%s target=%s", WHY_TARGET_GONE, entry, target)
            removed.append(str(entry))
            continue
        owner = _owner(entry)
        if not owner or _digest(owner, 16) != match.group(1):
            continue
        why = WHY_HOME_GONE if _gone(Path(owner).parent) else ""
        why = why or (WHY_SUPERSEDED if _superseded(owner) else "")
        if why and _remove(entry, why, owner):
            removed.append(str(entry))
    return removed


def sweep() -> list[str]:
    """Remove this user's stale short roots on this machine; return the removed paths."""

    paths.shared_uv_cache_dir()  # records this home's clio-u root, superseding its stand-in
    removed: list[str] = []
    seen: set[str] = set()
    for base in session_scratch.short_root_bases():
        key = os.path.normcase(str(base))
        if key not in seen and base.is_dir():
            seen.add(key)
            removed += _sweep_temp(base)
    local = os.environ.get("LOCALAPPDATA", "").strip() if os.name == "nt" else ""
    if local and Path(local).is_dir():
        removed += _sweep_local(Path(local))
    return removed


__all__ = ["WHY_HOME_GONE", "WHY_SUPERSEDED", "WHY_TARGET_GONE", "sweep"]
