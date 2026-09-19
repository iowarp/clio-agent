"""Per-OS spawn primitives for the standalone clio-core runtime daemon (owner module).

Extracted from ``arc/storage.py`` (#1148, no-accretion): the three pure helpers that
resolve the launcher binary, the shared-library env var, and the detach flags for
``clio_run start``. ``storage._spawn_runtime_daemon`` composes them (and stays in
``storage`` because it writes the daemon pidfile owned by the client-registry
lifecycle there); ``storage`` re-exports these names for existing callers/tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Optional


def _dynamic_library_env_var() -> str:
    """The OS env var the standalone launcher uses to find clio-core's shared libs."""
    if sys.platform == "darwin":
        return "DYLD_LIBRARY_PATH"
    if sys.platform.startswith("win"):
        return "PATH"  # Windows resolves DLLs via PATH
    return "LD_LIBRARY_PATH"


def _runtime_launcher_path(iowarp_core: object) -> Optional[str]:
    """Absolute path to the ``clio_run`` launcher (``.exe`` on Windows), or None."""
    bin_dir = iowarp_core.get_bin_dir()  # type: ignore[attr-defined]
    names = ("clio_run.exe", "clio_run") if sys.platform.startswith("win") else ("clio_run",)
    for name in names:
        candidate = os.path.join(bin_dir, name)
        if os.path.exists(candidate):
            return candidate
    return None


def _detached_popen_kwargs(*, breakaway: bool = True) -> "dict[str, Any]":
    """Popen kwargs that detach the daemon so it outlives the spawning process.

    POSIX: ``setsid``. Windows: ``CREATE_NO_WINDOW`` in a new process group, NOT
    ``DETACHED_PROCESS``: no console breaks the daemon's ZeroMQ Winsock init (#870).
    ``CREATE_BREAKAWAY_FROM_JOB`` (#900) breaks the shared daemon OUT of the server's
    ``KILL_ON_JOB_CLOSE`` Job Object so it survives a server hard-kill.

    That flag is only free when no Job Object is assigned, or when the assigned one
    permits breakaway. Inside a FOREIGN job that does not set ``BREAKAWAY_OK`` --
    a GitHub Actions runner, a sandbox, a managed Windows desktop -- ``CreateProcess``
    refuses the flag outright with ``ERROR_ACCESS_DENIED``. Callers that must spawn
    anyway pass ``breakaway=False`` and accept that the daemon now dies with the
    enclosing job; see ``storage._spawn_runtime_daemon``, which reports that
    downgrade rather than taking it silently.

    Args:
        breakaway: Whether to request ``CREATE_BREAKAWAY_FROM_JOB`` (Windows only).

    Returns:
        Keyword arguments for :class:`subprocess.Popen`.
    """
    if sys.platform.startswith("win"):
        flags = 0
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        if breakaway:
            flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}
