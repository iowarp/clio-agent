"""Install narrowly scoped optional dependencies for provider workflows."""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_ARGONNE_REQUIREMENT = "globus-sdk>=3.0.0"
# Exactly the locked SDK (uv.lock). An open floor would resolve the newest
# release, and claude-agent-sdk 0.2.157 ships no Windows wheel: its sdist
# installs without the bundled Claude Code CLI the SDK transport runs.
_CLAUDE_CODE_REQUIREMENT = "claude-agent-sdk==0.2.156"
_INSTALL_TIMEOUT_SECONDS = 180
_INSTALL_LOCK = threading.Lock()


class ProviderDependencyInstallError(RuntimeError):
    """Raised when CLIO cannot prepare an optional provider dependency."""


def _module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _uv_executable(python_executable: str) -> str | None:
    """Find uv beside a bundled runtime or on the backend host's PATH."""

    uv_name = "uv.exe" if os.name == "nt" else "uv"
    runtime_uv = Path(python_executable).resolve().parent.parent / "bin" / uv_name
    if runtime_uv.is_file():
        return str(runtime_uv)
    return shutil.which(uv_name)


def _install_command(python_executable: str, requirement: str) -> list[str]:
    uv = _uv_executable(python_executable)
    if uv:
        return [uv, "pip", "install", "--python", python_executable, requirement]
    return [python_executable, "-m", "pip", "install", requirement]


def _bounded_install_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "installer returned no diagnostic").strip()
    return detail[-1200:]


def _ensure_dependency(
    *,
    module_name: str,
    requirement: str,
    display_name: str,
    python_executable: str | None,
) -> bool:
    """Install one fixed optional dependency into the active backend runtime."""

    if _module_available(module_name):
        return False

    executable = python_executable or sys.executable
    with _INSTALL_LOCK:
        if _module_available(module_name):
            return False

        command = _install_command(executable, requirement)
        try:
            if os.name == "nt":
                result = subprocess.run(  # noqa: S603
                    command,
                    capture_output=True,
                    text=True,
                    timeout=_INSTALL_TIMEOUT_SECONDS,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                result = subprocess.run(  # noqa: S603
                    command,
                    capture_output=True,
                    text=True,
                    timeout=_INSTALL_TIMEOUT_SECONDS,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderDependencyInstallError(
                f"could not start the {display_name} installer: {exc}"
            ) from exc

        if result.returncode != 0:
            raise ProviderDependencyInstallError(
                f"{display_name} installation failed: " + _bounded_install_error(result)
            )

        importlib.invalidate_caches()
        if not _module_available(module_name):
            raise ProviderDependencyInstallError(
                f"{display_name} installation completed, but {requirement} is still "
                f"unavailable to {executable}."
            )
        return True


def ensure_argonne_support(*, python_executable: str | None = None) -> bool:
    """Ensure the active backend can run ALCF's Globus authentication.

    Returns ``True`` when support was installed during this call and ``False``
    when it was already present. The fixed dependency spec prevents request
    data from reaching the package installer.
    """

    return _ensure_dependency(
        module_name="globus_sdk",
        requirement=_ARGONNE_REQUIREMENT,
        display_name="ALCF support",
        python_executable=python_executable,
    )


def ensure_claude_code_support(*, python_executable: str | None = None) -> bool:
    """Ensure the active backend contains the Claude Agent SDK.

    Returns ``True`` when this call installed the SDK and ``False`` when it was
    already available. Only CLIO's fixed, audited requirement reaches the
    installer.
    """

    return _ensure_dependency(
        module_name="claude_agent_sdk",
        requirement=_CLAUDE_CODE_REQUIREMENT,
        display_name="Claude Code support",
        python_executable=python_executable,
    )
