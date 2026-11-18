"""How much disk the connected sources use, and how much is free, for the agent to see.

Downloads, snapshots and staging live in CLIO's source store. An agent that cannot see
them cannot judge whether a download fits or what it could release, so a full disk was
reclaimed by hand (R9). ``connected_data_status`` reports this block: free space where
the store lives, and each source's bytes on disk. A file hard-linked into several
snapshots (:func:`clio_agent.gact.storage.transfers._unchanged_files`) is counted once.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def bytes_on_disk(root: Path) -> int:
    """Bytes of the regular files under ``root``, each inode counted once."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for directory, _dirs, files in os.walk(root):
        for name in files:
            try:
                info = os.lstat(Path(directory) / name)
            except OSError:
                continue
            key = (info.st_dev, info.st_ino)
            if key in seen or not os.path.isfile(Path(directory) / name):
                continue
            seen.add(key)
            total += info.st_size
    return total


def storage_usage(store_root: Path, source_ids: list[str]) -> dict[str, Any]:
    """Free space where the source store lives and each listed source's local bytes."""
    usage = shutil.disk_usage(store_root if store_root.exists() else store_root.parent)
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "sources": {
            source_id: bytes_on_disk(store_root / source_id)
            for source_id in source_ids
            if (store_root / source_id).is_dir()
        },
    }
