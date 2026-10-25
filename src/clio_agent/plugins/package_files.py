"""The package file boundary shared by plugin hashing and installed snapshots."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

_IGNORED_NAMES = frozenset({".git", "__pycache__", ".venv", "node_modules"})


def ignored_package_names(directory: str, names: list[str]) -> set[str]:
    """Return generated names excluded from both snapshots and their digests."""
    del directory
    return {name for name in names if name in _IGNORED_NAMES or name.endswith(".pyc")}


def _raise_walk_error(error: OSError) -> None:
    """Never silently omit unreadable authored content from a trust digest."""
    raise error


def package_files(root: Path) -> Iterator[Path]:
    """Yield package files, pruning generated directories before entering them."""
    for directory, directories, files in os.walk(root, onerror=_raise_walk_error):
        directories[:] = sorted(set(directories) - ignored_package_names(directory, directories))
        for name in sorted(set(files) - ignored_package_names(directory, files)):
            path = Path(directory) / name
            if path.is_file():
                yield path
