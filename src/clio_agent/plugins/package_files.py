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


def source_stamp(root: Path) -> str:
    """The version of a local source as its file metadata: names, sizes and mtimes.

    Recorded at install and compared at start, so an unchanged source is recognised
    from one directory walk without reading or hashing any file (B4). A differing
    stamp means "maybe changed", never "changed": the caller then compares contents.
    """
    import hashlib  # noqa: PLC0415 - only the stamp needs it

    digest = hashlib.sha256()
    for path in package_files(root):
        info = path.stat()
        digest.update(
            f"{path.relative_to(root).as_posix()}\0{info.st_size}\0{info.st_mtime_ns}\n".encode()
        )
    return digest.hexdigest()
