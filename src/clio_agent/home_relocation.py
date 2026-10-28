"""A moved CLIO home stops using its old location (owner module).

CLIO keeps four roots (``config``, ``data``, ``state``, ``cache``; :mod:`clio_agent.paths`),
together under ``CLIO_AGENT_HOME`` or the OS home. Some files in them record absolute
paths into the roots. Moved with the home, those paths named the OLD home, so the moved
install read the old one's files and ran its programs, or failed once it was gone.

Each root carries ``.clio-root.json`` naming where it is. :func:`relocate_home` runs
when the server starts, before any store is opened; when a root's marker names
another place (or, for a home moved before markers existed, what the root's own records
say: a native service's ``owner.json`` for ``data``, an MCP environment's activation
scripts for ``cache``), the root was moved, and:

* the JSON records that name paths into the roots (``config/workspaces.json``,
  ``state/server/infrastructure.json``, each native service's ``owner.json``) have every
  path under an old root pointed at its new root;
* a native service's Python environment built at the old place is removed: its scripts
  start the old place's interpreter. The service then reads as not installed and is
  reinstalled from Infrastructure (``native_service_relocated``);
* MCP environments built at the old place are removed for clio-kit to rebuild
  (:mod:`clio_agent.tools.mcp_environment_relocation`).

Each moved root is logged ``clio_home_relocated`` with both paths and reported in
``/v1/health``. Nothing under the old home is opened. The plugin store relocates its
own records (:mod:`clio_agent.plugins.store_relocation`) and the clio-core store its own
(:mod:`clio_agent.arc.clio_core_relocate`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from clio_agent import path_relocation, paths

logger = logging.getLogger(__name__)

CLIO_HOME_RELOCATED = "clio_home_relocated"
NATIVE_SERVICE_RELOCATED = "native_service_relocated"
MARKER = ".clio-root.json"
_SERVICES = "services/*/*/owner.json"


def _roots() -> dict[str, Path]:
    return {
        "config": paths.user_config_dir(),
        "data": paths.user_data_dir(),
        "state": paths.user_state_dir(),
        "cache": paths.user_cache_dir(),
    }


def _pure(value: str) -> PurePath:
    return PureWindowsPath(value) if "\\" in value else PurePosixPath(value)


def _marker(root: Path) -> str | None:
    try:
        recorded = json.loads((root / MARKER).read_text(encoding="utf-8")).get("path")
    except (OSError, ValueError, AttributeError):
        return None
    return recorded if isinstance(recorded, str) and recorded else None


def _from_records(role: str, root: Path) -> str | None:
    """Where ``root`` was, from records that name their own location (no marker yet)."""
    if role == "data":
        for owner in sorted(root.glob(_SERVICES)):
            try:
                named = json.loads(owner.read_text(encoding="utf-8")).get("root")
            except (OSError, ValueError, AttributeError):
                continue
            pure = _pure(named) if isinstance(named, str) and named else None
            # <data>/services/<host>/<name>
            if pure is not None and pure.parts[-3:] == owner.parent.parts[-3:]:
                return str(pure.parents[2])
    if role == "cache":
        environments = root / "mcp-runtime" / "mcp-environments"
        for environment in sorted(environments.glob("*")) if environments.is_dir() else []:
            built_at = path_relocation.venv_recorded_path(environment)
            pure = _pure(built_at) if built_at else None
            # <cache>/mcp-runtime/mcp-environments/<name>
            if pure is not None and pure.parts[-3:] == environment.parts[-3:]:
                return str(pure.parents[2])
    return None


def _relocate_services(data: Path, moves: path_relocation.Moves) -> list[str]:
    changed: list[str] = []
    for owner in sorted(data.glob(_SERVICES)):
        if not path_relocation.relocate_json_file(owner, moves):
            continue
        changed.append(str(owner))
        environment = owner.parent / "environment" / ".venv"
        if environment.is_dir() and not environment.is_symlink():
            built_at = path_relocation.venv_recorded_path(environment)
            if path_relocation.venv_is_stale(environment):
                shutil.rmtree(environment)
                path_relocation.record(
                    {
                        "reason": NATIVE_SERVICE_RELOCATED,
                        "service": str(owner.parent),
                        "built_at": built_at,
                        "action": "environment removed; reinstall the service from Infrastructure",
                    }
                )
    return changed


def _write_marker(root: Path) -> None:
    if root.is_dir() and _marker(root) != str(root):
        path_relocation.atomic_write(
            root / MARKER, json.dumps({"schema": 1, "path": str(root)}).encode("utf-8")
        )


def relocate_home() -> list[dict[str, Any]]:
    """Point the moved roots' records at where they are now; the records logged.

    Returns:
        One ``clio_home_relocated`` record per moved root (empty when none moved).
    """
    roots = _roots()
    moved: dict[str, str] = {}
    for role, root in roots.items():
        if not root.is_dir():
            continue
        old = _marker(root) or _from_records(role, root)
        # Another spelling of the same directory (a symlinked home) is not a move.
        if old is not None and os.path.realpath(old) != os.path.realpath(root):
            moved[role] = old
    entries: list[dict[str, Any]] = []
    if moved:
        moves = [(old, str(roots[role])) for role, old in moved.items()]
        files = path_relocation.relocate_json_files(
            [
                roots["config"] / "workspaces.json",
                roots["state"] / "server" / "infrastructure.json",
            ],
            moves,
        )
        files += _relocate_services(roots["data"], moves)
        if "cache" in moved:
            from clio_agent.tools import mcp_environment_relocation as environments  # noqa: PLC0415

            files += environments.remove_stale_environments(environments.kit_cache_root())
        for role, old in moved.items():
            entry = {
                "reason": CLIO_HOME_RELOCATED,
                "role": role,
                "from": old,
                "to": str(roots[role]),
                "files": len(files),
            }
            path_relocation.record(entry)
            entries.append(entry)
    for root in roots.values():
        _write_marker(root)
    return entries
