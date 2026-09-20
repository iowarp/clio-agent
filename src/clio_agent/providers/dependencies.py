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


def _install_command(python_executable: str) -> list[str]:
    uv = _uv_executable(python_executable)
    if uv:
        return [uv, "pip", "install", "--python", python_executable, _ARGONNE_REQUIREMENT]
    return [python_executable, "-m", "pip", "install", _ARGONNE_REQUIREMENT]


def _bounded_install_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "installer returned no diagnostic").strip()
    return detail[-1200:]


def ensure_argonne_support(*, python_executable: str | None = None) -> bool:
    """Ensure the active backend can run ALCF's Globus authentication.

    Returns ``True`` when support was installed during this call and ``False``
    when it was already present. The fixed dependency spec prevents request
    data from reaching the package installer.
    """

    if _module_available("globus_sdk"):
        return False

    executable = python_executable or sys.executable
    with _INSTALL_LOCK:
        if _module_available("globus_sdk"):
            return False

        command = _install_command(executable)
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
                f"could not start the ALCF support installer: {exc}"
            ) from exc

        if result.returncode != 0:
            raise ProviderDependencyInstallError(
                "ALCF support installation failed: " + _bounded_install_error(result)
            )

        importlib.invalidate_caches()
        if not _module_available("globus_sdk"):
            raise ProviderDependencyInstallError(
                "ALCF support installation completed, but globus-sdk is still unavailable "
                f"to {executable}."
            )
        return True
