"""The one key rule for a workspace root, however it is spelled.

A stored ``root_path`` reaches this process in whatever form a config file, the
TUI, or a human typed it. The resident-fleet registry
(``ClioAgent._workspace_tool_executors`` / ``_workspace_leases``), the reaper,
``request_fleet_restart`` and every reader of that registry must agree on ONE key
or a genuinely-connected workspace looks untouched -- so the agreement lives in
this leaf module rather than in each caller's own
``str(Path(root).expanduser())``.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["canonical_workspace_root"]


def canonical_workspace_root(root: str | Path | None) -> str:
    """Return the ONE key a workspace root is filed under, however it is spelled.

    A stored ``root_path`` reaches this process in whatever form a config file,
    the TUI, or a human typed: ``~``-relative, trailing-separator, forward slashes
    on Windows. The resident-fleet registry (``ClioAgent._workspace_tool_executors``
    / ``_workspace_leases``) and every reader of it must agree on one key or a
    genuinely-connected workspace looks untouched -- so the agreement lives here
    rather than in each caller's own ``str(Path(root).expanduser())``.

    ``""`` means "no workspace bound" and is returned unchanged: it is a state,
    not a path to resolve (``Path("")`` would resolve to the process cwd).

    Deliberately PURE: ``expanduser`` + ``abspath`` collapse ``~``, ``.``/``..``
    and separators without touching the filesystem. ``Path.resolve()`` would also
    read symlinks, and this runs on the turn hot path — a workspace on a stalled
    network mount must not be able to block a resolve on a string operation.
    """

    text = str(root or "").strip()
    if not text:
        return ""
    return Path(os.path.abspath(os.path.expanduser(text))).as_posix()
