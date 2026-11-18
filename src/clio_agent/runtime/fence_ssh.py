"""Put the SSH launcher on a fenced shell's ``PATH`` (RL37).

Inside an active fence a shell's ``ssh``, ``scp`` and ``sftp`` resolve to small shims that run
:mod:`clio_agent.runtime.net_connect` on CLIO's interpreter. That launcher calls the real
program with the CONNECT helper as its ``ProxyCommand``, so the connection crosses the fence's
proxy, is recorded as ``net.egress`` with its host and port, and meets the workspace's network
policy. Git over SSH reaches the same shim through ``PATH``, or through ``GIT_SSH`` when the
person has not chosen an ssh command of their own.

On the floor (no fence) nothing changes: a direct ``ssh`` already works there.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import shutil
import sys
from pathlib import Path

from clio_agent.runtime import net_connect

logger = logging.getLogger(__name__)


def _helper() -> Path:
    return Path(net_connect.__file__).resolve()


def _shim_text(python: str, helper: str, shim_dir: str, tool: str) -> str:
    if os.name == "nt":
        argv = [python, "-I", helper, shim_dir, tool]
        quoted = " ".join('"' + item.replace("%", "%%") + '"' for item in argv)
        return f"@echo off\r\n{quoted} %*\r\n"
    argv = [python, "-I", helper, shim_dir, tool]
    return f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n'


def shim_directory() -> Path | None:
    """Write (once) and return the shim directory for this interpreter and helper.

    The directory name hashes the interpreter, the helper and the shim text, so an upgraded
    CLIO writes a fresh set and never runs a stale one. ``None`` when it cannot be written.
    """
    from clio_agent import paths  # noqa: PLC0415

    python, helper = sys.executable, str(_helper())
    seed = "\0".join([python, helper, _shim_text(python, helper, "", "ssh")])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    directory = paths.user_cache_dir() / "fence-ssh" / digest
    suffix = ".cmd" if os.name == "nt" else ""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for tool in net_connect.TOOLS:
            shim = directory / f"{tool}{suffix}"
            text = _shim_text(python, helper, str(directory), tool)
            if not shim.is_file() or shim.read_text(encoding="utf-8") != text:
                shim.write_text(text, encoding="utf-8")
            if os.name != "nt":
                shim.chmod(0o755)
    except OSError as exc:
        logger.warning(
            "fence ssh shims unavailable reason=fence_ssh_shims_unwritable dir=%s error=%s: %s",
            directory,
            type(exc).__name__,
            exc,
        )
        return None
    return directory


def fence_ssh_environment(environment: dict[str, str]) -> dict[str, str]:
    """The ``PATH`` and Git variables that route a fenced shell's SSH through the launcher.

    Empty when no fence is enforced, when the host has no ``ssh``, or when the shims cannot be
    written. ``CLIO_EXECUTION_PATH`` carries the shim directory too, so it survives a login
    profile that rebuilds ``PATH``.
    """
    from clio_agent.runtime.sandbox import fence_enforced  # noqa: PLC0415

    if not fence_enforced():
        return {}
    path = environment.get("PATH", os.environ.get("PATH", ""))
    if not shutil.which("ssh", path=path):
        return {}
    directory = shim_directory()
    if directory is None:
        return {}
    prefix = environment.get("CLIO_EXECUTION_PATH", "")
    overlay = {
        "PATH": os.pathsep.join(item for item in (str(directory), path) if item),
        "CLIO_EXECUTION_PATH": os.pathsep.join(item for item in (str(directory), prefix) if item),
    }
    if not (environment.get("GIT_SSH") or os.environ.get("GIT_SSH")):
        # Lowest of Git's ssh settings: GIT_SSH_COMMAND and core.sshCommand still win.
        overlay["GIT_SSH"] = str(directory / ("ssh.cmd" if os.name == "nt" else "ssh"))
    return overlay


__all__ = ["fence_ssh_environment", "shim_directory"]
