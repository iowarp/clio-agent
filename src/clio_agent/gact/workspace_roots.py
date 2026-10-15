"""Workspace root identity: one workspace per filesystem root (gact-tui#219).

Two workspace records over the same folder look like separate workspaces in the
UI while sharing file access and, by default, the same ``<root>/.clio`` runtime
storage. The store refuses that on create and on a root change; this module
decides when two roots are the same folder.

Roots are compared on the server's own filesystem, where
:func:`~clio_agent.gact.routes.workspace_root_materialization.materialize_workspace_root`
creates them. ``~``, ``..``, trailing separators and symlinks are resolved, and
``os.path.samefile`` covers case-insensitive filesystems. The stored root keeps
the spelling the user gave; only the comparison is canonical.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clio_agent.gact.workspaces import Workspace


class WorkspaceRootConflict(ValueError):
    """Raised when a root is already pinned by another workspace.

    Attributes:
        root_path: The root the caller asked for, as given.
        existing: The workspace that already pins that folder.
    """

    def __init__(self, root_path: str, existing: Workspace) -> None:
        super().__init__(
            f"workspace root {root_path!r} is already the root of workspace {existing.id}"
        )
        self.root_path = root_path
        self.existing = existing


def canonical_root(root_path: str) -> str:
    """Return the comparison key for a workspace root, or ``""`` for no root.

    Args:
        root_path: A root as stored or requested.

    Returns:
        The absolute, symlink-resolved, case-normalized path.
    """

    raw = root_path.strip()
    if not raw:
        return ""
    return os.path.normcase(os.path.realpath(os.path.expanduser(raw)))


def same_root(left: str, right: str) -> bool:
    """Whether two roots name the same folder. An empty root matches nothing.

    Args:
        left: One root.
        right: The other root.

    Returns:
        True when both are non-empty and resolve to the same directory.
    """

    left_key, right_key = canonical_root(left), canonical_root(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    try:
        return os.path.samefile(left_key, right_key)
    except OSError:
        # Either root is missing on disk, so the canonical strings decided it.
        return False
