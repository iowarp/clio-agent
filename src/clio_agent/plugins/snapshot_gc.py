"""Collect marketplace snapshots nothing refers to any more.

Every install or update of a marketplace copies its content to
``<plugin store>/snapshots/<market>/<digest>/`` and points the install state at it; an
update with new content makes a new digest and the old copy stays, so snapshots pile up
with every upgrade. This module removes the copies nothing can read again:

* a snapshot is **kept** while ``state.json`` names a path inside it (a marketplace's
  current snapshot, an active or pending plugin record, a git plugin's copy) or while
  any pinned session resolution (``resolved/<digest>.json``) does -- a conversation
  resolved against an older snapshot keeps reading its skills and servers from there;
* a snapshot (or an install's ``.partial-*`` staging folder) younger than the grace
  period is kept, so an install that copied its snapshot but has not yet written the
  state is never raced;
* everything else is **superseded** and removed, each with a typed reason.

It runs where the MCP cache prune runs, and under the same gate: at the end of an install
or upgrade (:func:`clio_agent.runtime.document_install.prune_after_install`) and in
``clio-agent doctor --gc`` (:func:`clio_agent.runtime.disk_gc.run_gc`), both of which
skip or refuse while any peer clio process (a running server) is alive. Server start
never walks the snapshots.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import FileLock

from clio_agent.platform_paths import rmtree_extended
from clio_agent.runtime import trace

__all__ = [
    "SNAPSHOT_GRACE_SECONDS",
    "SnapshotCollection",
    "collect_superseded_snapshots",
]

#: Seconds a fresh snapshot (or staging copy) is kept before it may be collected.
SNAPSHOT_GRACE_SECONDS = 600.0
#: Typed reasons of the collection rows.
REMOVED_SUPERSEDED = "snapshot_superseded"
REMOVED_ABANDONED_STAGING = "snapshot_staging_abandoned"


@dataclass
class SnapshotCollection:
    """What a collection removed (or, dry, would remove) and what it kept."""

    root: str
    dry_run: bool
    removed: list[dict[str, Any]] = field(default_factory=list)
    kept: int = 0

    @property
    def bytes_freed(self) -> int:
        return sum(int(row["bytes"]) for row in self.removed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "dry_run": self.dry_run,
            "kept": self.kept,
            "removed": self.removed,
            "bytes_freed": self.bytes_freed,
        }


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _normalized(path: str) -> str:
    text = path.replace("\\", "/")
    for prefix in ("//?/UNC/", "//?/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def _referenced(store_root: Path) -> set[tuple[str, str]]:
    """``(market, digest)`` of every snapshot a state or pinned-resolution path names.

    Raises:
        OSError / ValueError: a record could not be read -- the caller collects nothing
        rather than deleting a snapshot an unread record may name.
    """
    marker = _normalized(str((store_root / "snapshots").absolute())) + "/"
    documents = [store_root / "state.json"]
    resolved = store_root / "resolved"
    if resolved.is_dir():
        documents.extend(sorted(resolved.glob("*.json")))
    out: set[tuple[str, str]] = set()
    for document in documents:
        if not document.is_file():
            continue
        for text in _strings(json.loads(document.read_text(encoding="utf-8"))):
            at = _normalized(text).find(marker)
            if at < 0:
                continue
            parts = _normalized(text)[at + len(marker) :].split("/")
            if len(parts) >= 2 and parts[0] and parts[1]:
                out.add((parts[0], parts[1]))
    return out


def _installed_at(path: Path) -> float:
    """When ``path`` was copied in: the copy keeps its source's mtime, so the inode's
    change time (set by the copy and its rename) counts too."""
    stat = path.stat()
    return max(stat.st_mtime, stat.st_ctime)


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def collect_superseded_snapshots(
    store_root: Path,
    *,
    dry_run: bool = False,
    now: float | None = None,
    grace_seconds: float = SNAPSHOT_GRACE_SECONDS,
) -> SnapshotCollection:
    """Remove the snapshots under ``store_root`` that nothing refers to (see module doc).

    Holds the plugin store's state lock, so no install writes the state meanwhile. The
    caller gates it on "no running server" (the MCP cache prune's peer probe).

    Raises:
        OSError / ValueError: the state or a pinned resolution could not be read; nothing
        was removed.
    """
    now = time.time() if now is None else now
    result = SnapshotCollection(root=str(store_root), dry_run=dry_run)
    snapshots = store_root / "snapshots"
    if not snapshots.is_dir():
        return result
    with FileLock(str(store_root / "state.lock")):
        referenced = _referenced(store_root)
        for market in sorted(p for p in snapshots.iterdir() if p.is_dir()):
            for copy in sorted(p for p in market.iterdir() if p.is_dir()):
                staging = ".partial-" in copy.name
                if (market.name, copy.name) in referenced or (
                    now - _installed_at(copy) < grace_seconds
                ):
                    result.kept += 1
                    continue
                reason = REMOVED_ABANDONED_STAGING if staging else REMOVED_SUPERSEDED
                size = _size(copy)
                if not dry_run:
                    rmtree_extended(copy, ignore_errors=False)
                result.removed.append({"path": str(copy), "reason": reason, "bytes": size})
                trace.event(
                    "PLUGIN-GC",
                    "%s%s path=%s freed=%d",
                    "would-" if dry_run else "",
                    reason,
                    copy,
                    size,
                )
            if not dry_run and not any(market.iterdir()):
                market.rmdir()
    return result
