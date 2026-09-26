"""Install narrowly scoped optional dependencies for provider workflows."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from packaging.requirements import Requirement

#: The distribution CLIO's own extras (``pyproject.toml``
#: ``[project.optional-dependencies]``) are declared under -- the only source
#: a provider's install spec is ever resolved from (never request input).
_DISTRIBUTION_NAME = "clio-agent"

# Exactly the locked SDK (uv.lock), NOT resolved from the ``claude-code``
# extra's metadata marker: an open floor there (``>=0.2.156``) would resolve
# the newest release, and claude-agent-sdk 0.2.157 ships no Windows wheel --
# its sdist installs without the bundled Claude Code CLI the SDK transport
# runs. This one dependency stays a fixed, audited literal on purpose.
_CLAUDE_CODE_REQUIREMENT = "claude-agent-sdk==0.2.156"
_INSTALL_TIMEOUT_SECONDS = 180
_INSTALL_LOCK = threading.Lock()


class ProviderDependencyInstallError(RuntimeError):
    """Raised when CLIO cannot prepare an optional provider dependency."""


class ProviderExtraNotInstallableError(ProviderDependencyInstallError):
    """Raised for a provider kind with no declared installable extra.

    Distinct from a failed install: this is a typed refusal, never a silent
    no-op, for a provider that simply has nothing CLIO can install for it.
    """


def _module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _resolve_extra_requirements(
    extra_name: str, *, distribution_name: str = _DISTRIBUTION_NAME
) -> list[str]:
    """Read the package specs CLIO itself declared for ``extra_name``.

    Reads ``Requires-Dist`` off the INSTALLED distribution's own metadata
    (``importlib.metadata``, populated from ``pyproject.toml``
    ``[project.optional-dependencies]`` at build time) and keeps only the
    entries whose environment marker names this extra. This is the only
    place a provider's install command gets its package spec from -- never
    a request body, never a caller-supplied string.
    """

    try:
        declared = importlib.metadata.requires(distribution_name) or []
    except importlib.metadata.PackageNotFoundError as exc:
        raise ProviderDependencyInstallError(
            f"could not read {distribution_name}'s own package metadata: {exc}"
        ) from exc
    matches: list[str] = []
    for entry in declared:
        requirement = Requirement(entry)
        if requirement.marker is not None and requirement.marker.evaluate({"extra": extra_name}):
            matches.append(f"{requirement.name}{requirement.specifier}")
    return matches


def _uv_executable(python_executable: str) -> str | None:
    """Find uv beside a bundled runtime or on the backend host's PATH."""

    uv_name = "uv.exe" if os.name == "nt" else "uv"
    runtime_uv = Path(python_executable).resolve().parent.parent / "bin" / uv_name
    if runtime_uv.is_file():
        return str(runtime_uv)
    return shutil.which(uv_name)


def _install_command(python_executable: str, requirements: Sequence[str]) -> list[str]:
    uv = _uv_executable(python_executable)
    if uv:
        return [uv, "pip", "install", "--python", python_executable, *requirements]
    return [python_executable, "-m", "pip", "install", *requirements]


def _bounded_install_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "installer returned no diagnostic").strip()
    return detail[-1200:]


#: The real installer invocation, factored out so tests can substitute a fake
#: runner (``monkeypatch.setattr(dependencies, "_run_install_command", ...)``)
#: instead of ever spawning a real ``pip``/``uv`` process.
def _run_install_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        return subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        check=False,
    )


def _ensure_dependency(
    *,
    module_name: str,
    requirements: Sequence[str],
    display_name: str,
    python_executable: str | None,
) -> bool:
    """Install one or more fixed optional dependencies into the active backend runtime."""

    if _module_available(module_name):
        return False

    executable = python_executable or sys.executable
    with _INSTALL_LOCK:
        if _module_available(module_name):
            return False

        command = _install_command(executable, requirements)
        try:
            result = _run_install_command(command)
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
                f"{display_name} installation completed, but {', '.join(requirements)} is "
                f"still unavailable to {executable}."
            )
        return True


def ensure_provider_extra(
    *,
    extra_name: str,
    module_name: str,
    display_name: str,
    python_executable: str | None = None,
) -> bool:
    """Ensure the active backend has the package(s) CLIO declared under ``extra_name``.

    The install command's package spec comes ONLY from CLIO's own
    ``pyproject.toml`` extra (resolved fresh via :func:`_resolve_extra_requirements`
    on every call) -- never a request body, never a caller-supplied string. A
    provider extra CLIO never declared is a typed refusal
    (:class:`ProviderExtraNotInstallableError`), not a silent no-op.
    """

    requirements = _resolve_extra_requirements(extra_name)
    if not requirements:
        raise ProviderExtraNotInstallableError(
            f"clio-agent declares no packages under the '{extra_name}' extra"
        )
    return _ensure_dependency(
        module_name=module_name,
        requirements=requirements,
        display_name=display_name,
        python_executable=python_executable,
    )


def ensure_argonne_support(*, python_executable: str | None = None) -> bool:
    """Ensure the active backend can run ALCF's Globus authentication.

    Returns ``True`` when support was installed during this call and ``False``
    when it was already present. The package spec is resolved from CLIO's own
    ``argonne`` extra (see :func:`ensure_provider_extra`), never hardcoded here.
    """

    return ensure_provider_extra(
        extra_name="argonne",
        module_name="globus_sdk",
        display_name="ALCF support",
        python_executable=python_executable,
    )


def ensure_claude_code_support(*, python_executable: str | None = None) -> bool:
    """Ensure the active backend contains the Claude Agent SDK.

    Returns ``True`` when this call installed the SDK and ``False`` when it was
    already available. This ONE dependency stays a fixed, audited literal
    (``_CLAUDE_CODE_REQUIREMENT``) rather than resolving from the
    ``claude-code`` extra's own (looser) metadata marker -- see the constant's
    docstring for why an open floor is unsafe here.
    """

    return _ensure_dependency(
        module_name="claude_agent_sdk",
        requirements=[_CLAUDE_CODE_REQUIREMENT],
        display_name="Claude Code support",
        python_executable=python_executable,
    )


#: Provider kind -> its installer. The single generic seam the route dispatches
#: through (``ensure_provider_support``) so adding a new installable provider
#: is a registry entry here, never a new route-level special case.
_PROVIDER_INSTALLERS: Mapping[str, Callable[..., bool]] = {
    "argonne": ensure_argonne_support,
    "claude_code": ensure_claude_code_support,
}


def ensure_provider_support(provider_kind: str, *, python_executable: str | None = None) -> bool:
    """Install the optional runtime support ``provider_kind`` declares, if any.

    Raises :class:`ProviderExtraNotInstallableError` for a provider kind with
    no registered installer -- the route's 405 for "nothing to install" comes
    from this typed refusal, not a hardcoded provider-name check.
    """

    installer = _PROVIDER_INSTALLERS.get(provider_kind)
    if installer is None:
        raise ProviderExtraNotInstallableError(
            f"provider '{provider_kind}' has no installable runtime support"
        )
    return installer(python_executable=python_executable)
