"""MCP server environments built at another location are rebuilt, never run (owner module).

clio-kit builds each MCP server's Python environment under
``<kit cache>/mcp-environments/<server>-<hash>`` with uv. A uv environment is not
relocatable: its console scripts start ``<the location it was built at>/bin/python``.
Moved with the cache folder (a moved home or ``CLIO_AGENT_HOME``), the environment
started the OLD location's interpreter and imported the old location's packages; with
the old location gone the server could not start at all.

:func:`remove_stale_environments` runs before CLIO launches a clio-kit server (once per
process and cache): an environment whose activation scripts name another location
(:func:`clio_agent.path_relocation.venv_is_stale`) and that no live launcher holds is
removed, logged ``mcp_environment_relocated``, and clio-kit rebuilds it from its lock on
that launch. One a live launcher holds is left (``mcp_environment_stale_in_use``) and
removed on a later start. :func:`plan_targets_this_cache` is the matching check for the
learned direct launch (:mod:`clio_agent.tools.spawn_diet`).
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Sequence
from pathlib import Path

from clio_agent import path_relocation

logger = logging.getLogger(__name__)

MCP_ENVIRONMENT_RELOCATED = "mcp_environment_relocated"
MCP_ENVIRONMENT_STALE_IN_USE = "mcp_environment_stale_in_use"
_ENVIRONMENTS = "mcp-environments"

_swept: set[str] = set()
_swept_lock = threading.Lock()


def kit_cache_root(environment: dict[str, str] | None = None) -> Path:
    """The clio-kit cache CLIO points its launchers at (``CLIO_KIT_CACHE_DIR``)."""
    from clio_agent import paths  # noqa: PLC0415 - resolved per call (tests move it)

    configured = (environment or os.environ).get("CLIO_KIT_CACHE_DIR", "")
    return Path(configured) if configured else paths.user_cache_dir() / "mcp-runtime"


def remove_stale_environments(cache_root: Path) -> list[str]:
    """Remove the environments under ``cache_root`` built at another location (once).

    Returns:
        The environment names removed (empty after the first call for this cache).
    """
    key = os.path.realpath(cache_root)
    with _swept_lock:
        if key in _swept:
            return []
        _swept.add(key)
    root = cache_root / _ENVIRONMENTS
    if not root.is_dir():
        return []
    from clio_kit.environment_locks import environment_in_use  # noqa: PLC0415 - pinned dep

    removed: list[str] = []
    for environment in sorted(root.iterdir()):
        if environment.name.startswith(".") or environment.is_symlink():
            continue
        if not environment.is_dir() or not path_relocation.venv_is_stale(environment):
            continue
        built_at = path_relocation.venv_recorded_path(environment)
        if environment_in_use(cache_root, environment.name):
            logger.warning(
                "reason=%s environment=%s built_at=%s (a live launcher holds it; it is "
                "rebuilt once that launcher exits)",
                MCP_ENVIRONMENT_STALE_IN_USE,
                environment,
                built_at,
            )
            continue
        shutil.rmtree(environment)
        removed.append(environment.name)
        path_relocation.record(
            {
                "reason": MCP_ENVIRONMENT_RELOCATED,
                "environment": str(environment),
                "built_at": built_at,
                "action": "removed; clio-kit rebuilds it from its lock on the next launch",
            }
        )
    return removed


def plan_targets_this_cache(argv: Sequence[str], cache_root: Path) -> bool:
    """Whether a learned launch (``<env>/bin/python <env>/bin/<entry>``) runs an
    environment of THIS cache, built where it now is."""
    if len(argv) < 2:
        return False
    cache = os.path.realpath(cache_root)
    interpreter_dir = os.path.realpath(os.path.dirname(argv[0]))  # not the base interpreter
    for path in (interpreter_dir, os.path.realpath(argv[1])):
        try:
            if os.path.commonpath([cache, path]) != cache:
                return False
        except ValueError:  # another drive (Windows): not under the cache
            return False
    return not path_relocation.venv_is_stale(Path(interpreter_dir).parent)
