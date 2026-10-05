"""Filesystem primitives for deeply nested sources, including Windows long paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from clio_agent.platform_paths import win_extended_path


def walk(root: Path, *, topdown: bool = True) -> Iterator[tuple[Path, list[str], list[str]]]:
    """Walk without links and propagate unreadable directories instead of omitting data."""

    def failed(error: OSError) -> None:
        raise error

    base = win_extended_path(root)
    for directory, names, files in os.walk(
        base, topdown=topdown, followlinks=False, onerror=failed
    ):
        yield root / os.path.relpath(directory, base), names, files


def is_link(path: Path) -> bool:
    """Recognize both symlinks and Windows junctions at any path depth."""
    extended = win_extended_path(path)
    return os.path.islink(extended) or os.path.isjunction(extended)
