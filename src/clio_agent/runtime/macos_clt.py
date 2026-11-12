"""Treat the macOS Command Line Tools stubs as missing tools.

On a Mac without the Command Line Tools, ``/usr/bin/python3``, ``git``, ``make``,
``cc``, ``install_name_tool``, ``xcrun`` and about seventy more are stubs: running one
opens the "Install Command Line Developer Tools" dialog and then fails. Every stub
except ``xcrun`` is a hard link to one shim binary, so a tool is a stub when it is
that file (or ``xcrun``) and ``xcode-select -p`` names no developer directory.
``xcode-select -p`` itself never opens the dialog.

CLIO resolves such a stub as absent, so a missing tool fails with a clear message
instead of a modal dialog the person did not ask for.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from collections.abc import MutableMapping
from pathlib import Path

__all__ = [
    "command_line_tools_missing",
    "is_command_line_tools_stub",
    "real_which",
    "with_install_name_tool_stand_in",
]

#: A stub that is always present on macOS: every other stub except ``xcrun`` is this file.
_SHIM = Path("/usr/bin/cc")
_XCRUN = Path("/usr/bin/xcrun")


@functools.cache
def command_line_tools_missing() -> bool:
    """Whether this is a Mac with neither the Command Line Tools nor Xcode selected."""
    if sys.platform != "darwin":
        return False
    try:
        done = subprocess.run(
            ["/usr/bin/xcode-select", "-p"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Without a working xcode-select no developer directory is selected either.
        return True
    developer = done.stdout.strip()
    return done.returncode != 0 or not developer or not os.path.isdir(developer)


def _same_file(path: Path, other: Path) -> bool:
    try:
        return path.samefile(other)
    except OSError:
        return False


def is_command_line_tools_stub(path: str | os.PathLike[str]) -> bool:
    """Whether running ``path`` would open the Command Line Tools install dialog."""
    if not command_line_tools_missing():
        return False
    candidate = Path(path)
    return _same_file(candidate, _SHIM) or _same_file(candidate, _XCRUN)


def real_which(command: str, path: str | None = None) -> str | None:
    """``shutil.which`` that skips Command Line Tools stubs and keeps searching PATH."""
    found = shutil.which(command) if path is None else shutil.which(command, path=path)
    if not found or not is_command_line_tools_stub(found):
        return found
    if os.path.dirname(command):
        return None
    search = os.environ.get("PATH", os.defpath) if path is None else path
    for directory in search.split(os.pathsep):
        later = shutil.which(command, path=directory) if directory else None
        if later and not is_command_line_tools_stub(later):
            return later
    return None


def with_install_name_tool_stand_in(
    environment: MutableMapping[str, str],
) -> MutableMapping[str, str]:
    """Shadow the ``install_name_tool`` stub on ``environment``'s PATH with a failing stand-in.

    uv patches every Python it downloads with ``install_name_tool``; on a Mac without
    the Command Line Tools the stub opens their install dialog. The stand-in fails
    instead, so uv warns and keeps the interpreter unpatched. ``environment`` is merged
    over ``os.environ`` to read PATH and is updated in place; it is unchanged when the
    PATH's ``install_name_tool`` is real, absent or already the stand-in.
    """
    if not command_line_tools_missing():
        return environment
    path = {**os.environ, **environment}.get("PATH", "")
    found = next(
        (
            candidate
            for directory in path.split(os.pathsep)
            if directory
            and os.access(candidate := os.path.join(directory, "install_name_tool"), os.X_OK)
        ),
        None,
    )
    if found is None or not is_command_line_tools_stub(found):
        return environment
    from clio_agent import paths  # noqa: PLC0415 - paths reads the environment late

    tools = paths.user_cache_dir() / "clt-free-bin" / "install-name-tool"
    stand_in = tools / "install_name_tool"
    if not stand_in.is_file():
        tools.mkdir(parents=True, exist_ok=True)
        stand_in.write_text(
            "#!/bin/sh\necho 'clio: install_name_tool needs the Command Line Tools;"
            " the library is left as it is' >&2\nexit 1\n",
            encoding="utf-8",
        )
        stand_in.chmod(0o755)
    environment["PATH"] = os.pathsep.join(part for part in (str(tools), path) if part)
    return environment
