"""Put the SSH launcher on a fenced shell's ``PATH`` (RL37).

Inside an active fence a shell's ``ssh``, ``scp`` and ``sftp`` resolve to small shims that run
:mod:`clio_agent.runtime.net_connect` on CLIO's interpreter. That launcher calls the real
program with the CONNECT helper as its ``ProxyCommand``, so the connection crosses the fence's
proxy, is recorded as ``net.egress`` with its host and port, and meets the workspace's network
policy. Git over SSH reaches the same launcher through ``GIT_SSH_COMMAND``, which outranks the
person's ``core.sshCommand`` and ``GIT_SSH``; the launcher then runs the person's own ssh command
with the proxy added, so a configured ssh cannot dial past the fence.

On the floor (no fence) nothing changes: a direct ``ssh`` already works there.

On Windows each tool has a ``.cmd`` shim and, when distlib's console launcher is available, an
``.exe`` beside it. ``CreateProcess`` (Python's ``subprocess`` with a list, Node's ``spawn``
without a shell) only tries ``.exe`` for a bare name, so without the ``.exe`` such a spawn of
``ssh`` would skip the shim and dial straight out through System32 OpenSSH.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform
import shlex
import shutil
import struct
import sys
import zipfile
from importlib import resources
from pathlib import Path

from clio_agent.runtime import net_connect, net_git_ssh

logger = logging.getLogger(__name__)

#: The Windows shim directory's launcher for Git's ssh; no shell metacharacters in the name.
GIT_SSH_PROGRAM = "clio-git-ssh"


def _helper() -> Path:
    return Path(net_connect.__file__).resolve()


def _shim_text(python: str, helper: str, shim_dir: str, tool: str) -> str:
    if os.name == "nt":
        argv = [python, "-I", helper, shim_dir, tool]
        quoted = " ".join('"' + item.replace("%", "%%") + '"' for item in argv)
        return f"@echo off\r\n{quoted} %*\r\n"
    argv = [python, "-I", helper, shim_dir, tool]
    return f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n'


def _windows_launcher() -> bytes | None:
    """distlib's console launcher for this interpreter's architecture, or ``None``."""
    if struct.calcsize("P") == 8:
        bits = "64-arm" if platform.machine().lower() == "arm64" else "64"
    else:
        bits = "32"
    try:
        return resources.files("distlib").joinpath(f"t{bits}.exe").read_bytes()
    except (ImportError, OSError) as exc:
        logger.warning(
            "fence ssh exe shims unavailable reason=fence_ssh_launcher_missing error=%s: %s",
            type(exc).__name__,
            exc,
        )
        return None


def _exe_shim(launcher: bytes, python: str, helper: str, leading: list[str]) -> bytes:
    """A launcher + shebang + zipped ``__main__`` that runs ``helper`` with ``leading`` args.

    The launcher forwards argv, stdio and the exit code, and ties the interpreter to its own
    lifetime, exactly as a pip console script does.
    """
    main = (
        "import runpy, sys\n"
        f"sys.argv[:1] = {[helper, *leading]!r}\n"
        f"runpy.run_path({helper!r}, run_name='__main__')\n"
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(zipfile.ZipInfo("__main__.py", (1980, 1, 1, 0, 0, 0)), main)
    shebang = f'#!"{python}" -I\n'.encode()
    return launcher + shebang + archive.getvalue()


def _shims(python: str, helper: str, shim_dir: str) -> dict[str, bytes]:
    """Every shim file name and its bytes for this platform."""
    if os.name != "nt":
        return {
            tool: _shim_text(python, helper, shim_dir, tool).encode() for tool in net_connect.TOOLS
        }
    shims = {
        f"{tool}.cmd": _shim_text(python, helper, shim_dir, tool).encode()
        for tool in net_connect.TOOLS
    }
    launcher = _windows_launcher()
    if launcher is not None:
        for tool in net_connect.TOOLS:
            shims[f"{tool}.exe"] = _exe_shim(launcher, python, helper, [shim_dir, tool])
        git_helper = str(Path(net_git_ssh.__file__).resolve())
        shims[f"{GIT_SSH_PROGRAM}.exe"] = _exe_shim(launcher, python, git_helper, [shim_dir])
    return shims


def shim_directory() -> Path | None:
    """Write (once) and return the shim directory for this interpreter and helper.

    The directory name hashes the interpreter, the helper and the shims, so an upgraded
    CLIO writes a fresh set and never runs a stale one. ``None`` when it cannot be written.
    """
    from clio_agent import paths  # noqa: PLC0415

    python, helper = sys.executable, str(_helper())
    seed = hashlib.sha256("\0".join([python, helper]).encode("utf-8"))
    for name, content in sorted(_shims(python, helper, "").items()):
        seed.update(name.encode("utf-8") + b"\0" + content)
    directory = paths.user_cache_dir() / "fence-ssh" / seed.hexdigest()[:16]
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in _shims(python, helper, str(directory)).items():
            shim = directory / name
            if not shim.is_file() or shim.read_bytes() != content:
                shim.write_bytes(content)
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
    overlay.update(_git_ssh_overlay(environment, directory))
    return overlay


def git_ssh_command(directory: Path) -> str:
    """The fence's ``GIT_SSH_COMMAND``, which runs :mod:`net_git_ssh`.

    Git starts a ``GIT_SSH_COMMAND`` that has no shell metacharacters directly, without
    ``sh -c``. Git for Windows' ``sh`` cannot start inside the native Windows fence (its MSYS
    runtime is refused a named-object directory), so there the command is the bare name of the
    shim directory's launcher, which ``PATH`` resolves.
    """
    if (directory / f"{GIT_SSH_PROGRAM}.exe").is_file():
        return GIT_SSH_PROGRAM
    helper = Path(net_git_ssh.__file__).resolve()
    return shlex.join([sys.executable, "-I", str(helper), str(directory)])


def _git_ssh_overlay(environment: dict[str, str], directory: Path) -> dict[str, str]:
    """``GIT_SSH_COMMAND`` set to the launcher, keeping the person's own ssh choice.

    ``GIT_SSH_COMMAND`` outranks every other Git ssh setting, so the person's
    ``core.sshCommand`` or ``GIT_SSH`` no longer bypasses the shim. The launcher still runs the
    person's command, read again at each call so a repository's own ``core.sshCommand`` counts;
    the person's variables are kept in ``CLIO_PERSON_GIT_SSH*`` for it.
    """
    ours = git_ssh_command(directory)
    overlay = {"GIT_SSH_COMMAND": ours}
    shim = net_connect.shim_ssh(str(directory))
    for name, keep in (
        ("GIT_SSH_COMMAND", net_git_ssh.PERSON_GIT_SSH_COMMAND),
        ("GIT_SSH", net_git_ssh.PERSON_GIT_SSH),
    ):
        value = environment.get(name, os.environ.get(name, ""))
        kept = environment.get(keep, os.environ.get(keep, ""))
        if value and value not in {ours, shim}:
            overlay[keep] = value
        elif kept:
            overlay[keep] = kept
    return overlay


__all__ = ["fence_ssh_environment", "shim_directory"]
