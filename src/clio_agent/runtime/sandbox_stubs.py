"""Recognise the empty placeholders a codex sandbox mounts into writable roots.

While a codex (bubblewrap) sandboxed command runs, it protects ``.git``,
``.codex``, ``.agents`` and ``.aws`` in every writable root by mounting an empty
read-only file or directory there, creating the entry when it is missing. Those
placeholders are not the user's data, so CLIO's file listings omit them. Only an
empty entry qualifies: a real repository's non-empty ``.git`` is never a stub.
"""

from __future__ import annotations

import os
from pathlib import Path

SANDBOX_MOUNT_STUB_NAMES = frozenset({".git", ".codex", ".agents", ".aws"})


def is_sandbox_mount_stub(path: Path) -> bool:
    """Return whether ``path`` is an empty sandbox placeholder; a non-empty ``.git`` never is."""

    if path.name not in SANDBOX_MOUNT_STUB_NAMES:
        return False
    try:
        if path.is_symlink():
            return False
        if path.is_dir():
            with os.scandir(path) as children:
                return next(children, None) is None
        return path.is_file() and path.stat().st_size == 0
    except OSError:
        return False


__all__ = ["SANDBOX_MOUNT_STUB_NAMES", "is_sandbox_mount_stub"]
