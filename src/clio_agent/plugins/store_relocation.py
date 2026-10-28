"""A moved plugin store keeps loading its own snapshots (owner module).

``state.json`` and every pinned resolution (``resolved/<digest>.json``) record plugin
roots, skill, fragment and command files and MCP server arguments as absolute paths
into ``<store>/snapshots``. Moved with its configuration folder, the store still
named the old snapshots, so a moved home loaded its plugins (and ran their code) from
the old location.

The store's location is read from what it recorded: a marketplace snapshot is always
``<store>/snapshots/<market>/<digest>``. When that names another store directory,
:func:`relocate_plugin_store` rewrites every path under it in ``state.json`` and the
pinned resolutions (a resolution's digest leaves locations out, so pins stay valid)
and logs ``plugin_store_relocated``. Nothing under the old store is opened.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from clio_agent import path_relocation

PLUGIN_STORE_RELOCATED = "plugin_store_relocated"

_checked: set[str] = set()
_checked_lock = threading.Lock()


def recorded_store_root(state: dict[str, Any]) -> str | None:
    """The store directory ``state`` was written in, from its marketplace snapshots."""
    for entry in (state.get("marketplaces") or {}).values():
        snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
        if not isinstance(snapshot, str) or not snapshot:
            continue
        pure: PurePath = PureWindowsPath(snapshot) if "\\" in snapshot else PurePosixPath(snapshot)
        if len(pure.parents) >= 3 and pure.parents[1].name == "snapshots":
            return str(pure.parents[2])
    return None


def _recorded_root(state_path: Path) -> str | None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # absent, or unreadable: the store reports that when it reads it
    return recorded_store_root(state) if isinstance(state, dict) else None


def relocate_plugin_store(root: Path, lock: Any) -> dict[str, Any] | None:
    """Point a moved store's recorded paths at ``root`` once per process; the record.

    Args:
        root: The store directory (``<config>/plugins`` or ``plugins.store_root``).
        lock: The store's file lock, held while its files are rewritten.

    Returns:
        The ``plugin_store_relocated`` record, or ``None`` when nothing moved.
    """
    key = str(root)
    with _checked_lock:
        if key in _checked:
            return None
        _checked.add(key)
    state_path = root / "state.json"
    old = _recorded_root(state_path)
    if old is None or old == str(root):
        return None  # the usual case: no lock taken
    with lock:
        old = _recorded_root(state_path)  # another process may have relocated it meanwhile
        if old is None or old == str(root):
            return None
        files = path_relocation.relocate_json_files(
            [state_path, *sorted((root / "resolved").glob("*.json"))], [(old, str(root))]
        )
    entry = {"reason": PLUGIN_STORE_RELOCATED, "from": old, "to": str(root), "files": len(files)}
    path_relocation.record(entry)
    return entry
