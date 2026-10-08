"""Select bundled execution tools and expose prepared tools to workspace shells."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

from clio_agent.runtime.github_cli import installed_github_cli

_ENVIRONMENTS: dict[Path, dict[str, str]] = {}
_LOCK = RLock()


def bundled_root() -> Path | None:
    """Find the desktop runtime independent of its installation directory."""
    configured = os.environ.get("GACT_BUNDLED_RUNTIME_DIR", "")
    candidates = [Path(configured)] if configured else []
    candidates.extend(Path(sys.executable).resolve().parents[:4])
    return next((root for root in candidates if (root / "runtime.json").is_file()), None)


def _wrapper(path: Path, argv: list[str]) -> None:
    if os.name == "nt":
        quoted = " ".join('"' + item.replace("%", "%%") + '"' for item in argv)
        path.with_suffix(".cmd").write_text(
            f"@echo off\r\nchcp 65001 >nul\r\n{quoted} %*\r\n", encoding="utf-8"
        )
    else:
        path.write_text(f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n', encoding="utf-8")
        path.chmod(0o755)


def publish_environment(workspace: Path, runtime: dict[str, Any], cache: Path) -> None:
    """Bind verified interpreter paths to future shell calls in this workspace."""
    from clio_agent.runtime.document_stack.process import scratch_root

    scratch = str(scratch_root(workspace))
    tools = cache / "shell-bin"
    tools.mkdir(exist_ok=True)
    _wrapper(tools / "python", runtime["python_argv"])
    _wrapper(tools / "python3", runtime["python_argv"])
    directories = [str(tools), str(Path(runtime["uv"]).parent)]
    javascript = runtime["javascript"]
    if javascript["status"] == "ready":
        _wrapper(tools / "pnpm", javascript["pnpm_argv"])
        directories.append(str(Path(runtime["node"]).parent))
    environment = {
        "PATH": os.pathsep.join([*directories, os.environ.get("PATH", "")]),
        "CLIO_EXECUTION_PATH": os.pathsep.join(directories),
        "CLIO_EXECUTION_PYTHON": runtime["python"],
        "CLIO_DOCUMENT_OFFICE_ROOT": runtime["office_root"],
        "CLIO_DOCUMENT_SCRATCH": scratch,
        "CLIO_DOCUMENT_SHORT_TEMP": tempfile.gettempdir(),
        "TMPDIR": scratch,
        "TEMP": scratch,
        "TMP": scratch,
    }
    runtime["scratch_directory"] = scratch
    runtime["shell_environment"] = environment
    with _LOCK:
        _ENVIRONMENTS[workspace.resolve()] = environment


def shell_environment(workspace: Path) -> dict[str, str]:
    """Return only this process's verified workspace binding, never workspace input."""
    with _LOCK:
        environment = dict(_ENVIRONMENTS.get(workspace.resolve(), {}))
    path = environment.get("PATH", os.environ.get("PATH", ""))
    if shutil.which("gh", path=path):
        return environment
    executable = installed_github_cli()
    if executable is not None:
        directory = str(executable.parent)
        environment["PATH"] = os.pathsep.join([directory, path])
        prefix = environment.get("CLIO_EXECUTION_PATH", "")
        environment["CLIO_EXECUTION_PATH"] = os.pathsep.join(
            part for part in (directory, prefix) if part
        )
    return environment


def posix_command(command: str, environment: dict[str, str]) -> str:
    """Restore managed tools after login profiles initialize the host's PATH."""
    prefix = environment.get("CLIO_EXECUTION_PATH", "")
    return f'export PATH={shlex.quote(prefix)}:"$PATH"\n{command}' if prefix else command
