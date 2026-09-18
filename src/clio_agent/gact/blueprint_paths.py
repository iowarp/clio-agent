"""Filesystem-identity helpers for installed Agent Blueprints.

Split out of ``gact/agent_blueprints.py`` (file-size ratchet, #775/#774) --
this module owns the small, pure path computations that installed-blueprint
code (install/uninstall, refresh, source resolution, and the blueprint-file
listing that walks an installed tree) all share: where a blueprint SCOPE
(``global`` / ``workspace``) is rooted on disk, and how to compute a path
relative to a blueprint root that survives Windows packaged-path redirection.

Both names stay importable from ``clio_agent.gact.agent_blueprints`` under
their historical private names (``_install_root`` / ``_relative_to_blueprint_root``)
because ``agent_blueprint_refresh.py`` and ``agent_blueprint_sources.py`` already
import ``_install_root`` directly from there.
"""

from __future__ import annotations

import os
from pathlib import Path


def install_root(*, home: Path, cwd: Path, scope: str) -> Path:
    """Return the on-disk root a blueprint SCOPE installs into.

    Args:
        home: The user's home directory (test seam; production passes the real one).
        cwd: The current working directory, for the ``workspace`` scope.
        scope: Either ``"global"`` (per-user config dir) or ``"workspace"``
            (``.clio/agent-blueprints`` under ``cwd``).

    Returns:
        The directory blueprints of that scope install under.

    Raises:
        ValueError: ``scope`` is neither ``"global"`` nor ``"workspace"``.
    """
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    config_root = paths.user_config_dir_for(home, os.environ)
    if scope == "global":
        return config_root / "agent-blueprints"
    if scope == "workspace":
        return cwd / ".clio" / "agent-blueprints"
    raise ValueError("scope must be global or workspace")


def relative_to_blueprint_root(path: Path, root: Path) -> Path:
    """Return a safe relative path across Windows packaged-path redirection.

    A process descended from a packaged Windows app can enumerate an ordinary
    ``%LOCALAPPDATA%`` file through the package's ``LocalCache\\Local`` alias.
    ``Path.resolve()`` then rewrites the child but not necessarily its blueprint
    root, so a plain ``relative_to`` rejects two paths that identify the same
    on-disk tree.  The file-identity fallback accepts only an equivalent root;
    a symlink that actually escapes the blueprint remains rejected.
    """

    lexical = path.relative_to(root)
    resolved = path.resolve()
    resolved_root = root.resolve()
    try:
        return resolved.relative_to(resolved_root)
    except ValueError:
        equivalent_root = resolved
        for _part in lexical.parts:
            equivalent_root = equivalent_root.parent
        if os.path.samefile(equivalent_root, resolved_root):
            return lexical
        raise
