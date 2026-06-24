"""Single source of truth for clio-agent's on-disk artifact locations.

Two clearly-delimited kinds of root:

* **WORKSPACE** — ``<cwd>/.clio`` — per-project artifacts that belong to the workspace,
  split into ``agent/`` (clio-agent: ARC, sessions, traces, messages, context-files) and
  ``core/`` (clio-core / the CTE runtime: config, any file-tier output).
* **USER** — per-user, shared across every workspace, resolved **OS-correctly via
  ``platformdirs``** (Linux ``~/.config/clio-agent`` honoring ``XDG_CONFIG_HOME``; macOS
  ``~/Library/Application Support/clio-agent``; Windows ``%APPDATA%\\clio-agent``):
    * :func:`user_config_dir` — user content/state (custom agents, workspace registry,
      installed blueprints + expert-packs, hooks, prompts, config). The valuable stuff.
    * :func:`user_cache_dir` — regenerable caches (the models.dev catalog). Safe to wipe.

Every default path in clio-agent resolves through this module, so there is exactly one
place that decides where artifacts live (no scattered ``~/.config`` literals that silently
break on macOS/Windows). ``CLIO_USER_DIR`` overrides the per-user root (tests / power users).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import platformdirs

_APP = "clio-agent"


def _user_override() -> "Path | None":
    raw = os.environ.get("CLIO_USER_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def user_config_dir() -> Path:
    """OS-correct per-user config/content dir for clio-agent (resolved from the real env).

    Linux ``~/.config/clio-agent`` (honors ``XDG_CONFIG_HOME``), macOS
    ``~/Library/Application Support/clio-agent``, Windows ``%LOCALAPPDATA%\\clio-agent``.
    Overridable with ``CLIO_USER_DIR``. Callers that inject a fake ``home``/``env`` for
    tests (conf, mcp_config, blueprints, …) must use :func:`user_config_dir_for` instead —
    ``platformdirs`` reads the real process environment and cannot honor an injected one.
    """
    override = _user_override()
    if override is not None:
        return override
    return Path(platformdirs.user_config_dir(_APP, appauthor=False))


def user_config_dir_for(home: Path, env: Mapping[str, str]) -> Path:
    """Per-user config dir resolved from an INJECTED ``home`` + ``env`` (the DI variant).

    Same precedence + OS layout as :func:`user_config_dir` (``CLIO_USER_DIR`` →
    ``XDG_CONFIG_HOME`` → OS-native: macOS ``~/Library/Application Support/clio-agent``,
    Windows ``%LOCALAPPDATA%\\clio-agent``, else ``~/.config/clio-agent``) — but
    parameterized so callers that inject a fake home/env for tests keep working (in
    production, ``home=Path.home()`` + ``env=os.environ`` and this matches
    :func:`user_config_dir`). The OS layout is mirrored by hand here because
    ``platformdirs`` cannot resolve against an injected env.
    """
    override = (env.get("CLIO_USER_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg) / _APP
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / _APP
    if os.name == "nt":
        return Path(env.get("LOCALAPPDATA") or env.get("APPDATA") or str(home)) / _APP
    return home / ".config" / _APP


def user_cache_dir() -> Path:
    """OS-correct per-user cache dir for regenerable artifacts (e.g. the models.dev catalog).

    Linux ``~/.cache/clio-agent``, macOS ``~/Library/Caches/clio-agent``, Windows
    ``%LOCALAPPDATA%\\clio-agent\\Cache``. Overridable with ``CLIO_USER_DIR`` (``/cache``).
    """
    override = _user_override()
    if override is not None:
        return override / "cache"
    return Path(platformdirs.user_cache_dir(_APP, appauthor=False))


def workspace_clio(cwd: "str | Path | None" = None) -> Path:
    """The workspace clio root: ``<cwd>/.clio``."""
    return (Path(cwd) if cwd is not None else Path.cwd()) / ".clio"


def workspace_agent_dir(cwd: "str | Path | None" = None) -> Path:
    """Per-workspace clio-agent artifacts: ``<cwd>/.clio/agent`` (ARC, sessions, traces)."""
    return workspace_clio(cwd) / "agent"


def workspace_core_dir(cwd: "str | Path | None" = None) -> Path:
    """Per-workspace clio-core artifacts: ``<cwd>/.clio/core`` (CTE config / file tiers)."""
    return workspace_clio(cwd) / "core"
