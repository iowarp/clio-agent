"""Single source of truth for clio's on-disk artifact roots.

clio writes into two clearly-delimited roots, each split into ``agent/`` (clio-agent's
own artifacts) and ``core/`` (clio-core / the CTE runtime):

* **WORKSPACE** — ``<cwd>/.clio`` — per-project artifacts that belong to the workspace:
  ARC data (``.clio/agent/arc``), the session registry, per-session traces + messages,
  context-file metadata, permission policies. ``.clio/agent`` is clio-agent; ``.clio/core``
  is clio-core (CTE config / any file-tier output).
* **USER** — per-user, OS-correct (``~/.clio`` on POSIX, ``%APPDATA%/clio`` on Windows) —
  artifacts shared across every workspace: the model-catalog cache and user-level config.
  clio-core already seeds ``~/.clio/clio.yaml`` here, so ``~/.clio`` is the natural home.

Every default path in clio-agent resolves through this module so there is exactly one
place that decides "where do clio artifacts live", instead of literals scattered across
the tree. The OS-correct user base uses ``platformdirs`` for the Windows ``%APPDATA%`` case.
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

_APP = "clio"


def user_clio_home() -> Path:
    """Per-user clio home: ``~/.clio`` on POSIX, ``%APPDATA%/clio`` on Windows.

    POSIX deliberately uses ``~/.clio`` (not the XDG ``~/.local/share``) so it matches
    clio-core's existing ``~/.clio/clio.yaml``; Windows resolves ``%APPDATA%/clio`` via
    ``platformdirs`` (the cross-platform OS-correct base).
    """
    if os.name == "nt":
        return Path(platformdirs.user_config_dir(_APP, appauthor=False, roaming=True))
    return Path.home() / ".clio"


def user_agent_dir() -> Path:
    """Per-user clio-agent artifacts (model cache, user-level state): ``<user>/agent``."""
    return user_clio_home() / "agent"


def user_core_dir() -> Path:
    """Per-user clio-core artifacts (e.g. ``clio.yaml``): ``<user>/core``."""
    return user_clio_home() / "core"


def workspace_clio(cwd: "str | Path | None" = None) -> Path:
    """The workspace clio root: ``<cwd>/.clio``."""
    return (Path(cwd) if cwd is not None else Path.cwd()) / ".clio"


def workspace_agent_dir(cwd: "str | Path | None" = None) -> Path:
    """Per-workspace clio-agent artifacts: ``<cwd>/.clio/agent`` (ARC, sessions, traces)."""
    return workspace_clio(cwd) / "agent"


def workspace_core_dir(cwd: "str | Path | None" = None) -> Path:
    """Per-workspace clio-core artifacts: ``<cwd>/.clio/core`` (CTE config / file tiers)."""
    return workspace_clio(cwd) / "core"
