"""Where the document environment's venv and uv cache live, relative to this interpreter."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _built_for_this_interpreter(environment: Path) -> bool:
    """Whether a cached venv still derives from the interpreter running Clio.

    The cache key covers the Python version and lock files, not the installation path. A
    macOS app first opened translocated (from the DMG, Downloads or a quarantined copy),
    then moved to /Applications, leaves a venv whose ``home`` no longer exists, or one
    that silently keeps using an older copy of the app that still exists.
    """
    try:
        lines = (environment / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return True  # Nothing recorded to compare; the import probe still verifies it.
    recorded = next(
        (
            value.strip()
            for key, _, value in (line.partition("=") for line in lines)
            if key.strip() == "home"
        ),
        "",
    )
    if not recorded:
        return True
    try:
        home = Path(recorded).resolve(strict=True)
    except OSError:
        return False
    base = Path(sys.base_prefix)
    current = {
        Path(sys.executable).resolve().parent,
        Path(getattr(sys, "_base_executable", sys.executable)).resolve().parent,
        base.resolve(),
        (base / "bin").resolve(),
    }
    return home in current


def _set_environment_home(environment: Path) -> None:
    """Point a relocated venv at this interpreter; Windows launchers need an absolute home.

    Run from a virtualenv, that is the virtualenv's base interpreter: a venv whose home
    is another venv's launcher directory runs as that other venv, not as itself.
    """
    config = environment / "pyvenv.cfg"
    home = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve().parent)
    lines = config.read_text(encoding="utf-8").splitlines()
    config.write_text(
        "\n".join(
            f"home = {home}" if line.partition("=")[0].strip() == "home" else line for line in lines
        )
        + "\n",
        encoding="utf-8",
    )


def _uv_cache_dir(uv: str) -> Path | None:
    """The cache ``uv`` uses by default here (``uv cache dir``), or None if it cannot say."""
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        process = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [uv, "cache", "dir"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **flags,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = Path(process.stdout.strip()) if process.returncode == 0 else None
    return path if path is not None and path.is_absolute() else None


def _installer_wheel_root() -> Path | None:
    """The installer's downloads folder when it holds a runtime archive's document wheels.

    ``install.sh`` installs a Linux release from its prebuilt runtime archive and hands the
    archive's ``document-wheels`` over here, so this environment is laid out from the same
    locked wheels the desktop uses instead of downloading them again.
    ``bundled_wheel_requirements`` checks every wheel against this stack's lock.
    """
    from clio_agent.runtime.document_stack.office import INSTALLER_DOWNLOADS

    folder = os.environ.get(INSTALLER_DOWNLOADS, "")
    root = Path(folder) if folder else None
    return root if root and (root / "document-wheels" / "manifest.json").is_file() else None


def _filesystem(path: Path) -> int | None:
    """The device holding ``path``'s nearest existing ancestor, if it is writable."""
    for candidate in (path, *path.parents):
        if os.path.exists(candidate):
            return os.stat(candidate).st_dev if os.access(candidate, os.W_OK) else None
    return None


def _document_uv_cache(uv: str, home: Path, *, bundled: bool) -> Path:
    """The uv cache for the document environment beside ``home``.

    A release lays the environment out from the runtime's bundled wheels, so its cache is
    CLIO's own: the ~500 MB it fills go with ``uninstall --purge-caches`` instead of
    staying in ``~/.cache/uv`` after CLIO is gone. A source install reuses uv's own cache
    when it is on the same filesystem: the agent's ``uv sync`` already downloaded numpy,
    matplotlib, lxml and the rest there, so they are linked rather than downloaded again.
    A cache on another drive would copy every file instead, so that keeps the private one.
    """
    private = home / "uv-document-cache"
    if bundled:
        return private
    shared = _uv_cache_dir(uv)
    if shared is None:
        return private
    device = _filesystem(shared)
    return shared if device is not None and device == _filesystem(home) else private
