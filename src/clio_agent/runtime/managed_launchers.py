"""Resolve the launchers CLIO ships from CLIO's own environment, never from PATH.

CLIO pins clio-kit, and every console script its environment's packages declare, as a
dependency: readiness, provisioning and the connector catalogue all read that pinned
copy. Looking a launcher up on PATH instead ran whatever the person installed under the
same name (a global ``uv tool``, an editable checkout of another branch), a different
version with different servers, so a run depended on the person's PATH.
:func:`resolve_launcher` looks in CLIO's own places first, the same way on every OS:

1. a scoped installation's own tool folder (``<prefix>/uv-tools/bin``,
   :mod:`clio_agent.runtime.installation_tools`);
2. the scripts folder of the environment running CLIO, for a console script one of its
   installed distributions declares.

A launcher CLIO's environment declares but whose script is missing is not looked up on
PATH (``None``: the caller reports it missing). Only a command CLIO does not ship is
looked up on PATH.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from functools import lru_cache
from pathlib import Path

from clio_agent.runtime import trace

__all__ = ["managed_launcher", "resolve_launcher", "ships_launcher"]


@lru_cache(maxsize=1)
def _declared_scripts() -> frozenset[str]:
    """The console scripts the distributions of CLIO's environment declare."""

    from importlib import metadata  # noqa: PLC0415

    try:
        return frozenset(point.name for point in metadata.entry_points(group="console_scripts"))
    except Exception as exc:  # noqa: BLE001 - a broken distribution must not stop spawns
        trace.event("TOOLS", "managed_launchers_unreadable error=%s", exc)
        return frozenset()


def _script_names(command: str) -> tuple[str, ...]:
    return (f"{command}.exe", command) if os.name == "nt" else (command,)


def _first_script(folder: Path, command: str) -> str | None:
    for name in _script_names(command):
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    return None


def _environment_script_dirs() -> list[Path]:
    """The folders the environment running CLIO installs console scripts into."""

    folders: list[Path] = []
    for value in (sysconfig.get_path("scripts"), str(Path(sys.executable).parent)):
        if value and Path(value) not in folders:
            folders.append(Path(value))
    return folders


def ships_launcher(command: str) -> bool:
    """Whether ``command`` is a console script CLIO's environment ships."""

    return bool(command) and not os.path.dirname(command) and command in _declared_scripts()


def managed_launcher(command: str) -> str | None:
    """The copy of ``command`` CLIO ships, else ``None``.

    A scoped installation's ``uv-tools/bin`` comes first (its CLI launchers run that
    copy), then the scripts folder of CLIO's own environment.
    """

    if not command or os.path.dirname(command):
        return None
    from clio_agent.runtime.installation_tools import installation_prefix  # noqa: PLC0415

    prefix = installation_prefix()
    if prefix is not None:
        found = _first_script(prefix / "uv-tools" / "bin", command)
        if found is not None:
            return found
    if not ships_launcher(command):
        return None
    for folder in _environment_script_dirs():
        found = _first_script(folder, command)
        if found is not None:
            return found
    return None


def resolve_launcher(command: str, path: str | None = None) -> str | None:
    """The executable ``command`` runs: CLIO's own copy, else (unshipped) PATH's.

    Args:
        command: The declared launcher (a bare name or a path).
        path: The PATH to search for a command CLIO does not ship (the process's own
            when ``None``).
    """

    from clio_agent.runtime.macos_clt import real_which  # noqa: PLC0415

    found = managed_launcher(command)
    if found is not None:
        return found
    if ships_launcher(command):
        trace.event("TOOLS", "managed_launcher_missing command=%s", command)
        return None
    return real_which(command) if path is None else real_which(command, path)
