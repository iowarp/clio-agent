"""Which Copilot runtime the SDK transport spawns: the user's ``copilot`` or the staged one.

``github-copilot-sdk`` pins a runtime release (``copilot._cli_version.CLI_VERSION``)
and stages it in a per-user cache on ``python -m copilot download-runtime``
(CLIO runs that on Install, :func:`clio_agent.providers.dependencies
.ensure_copilot_support`). The user's own ``copilot`` CLI is preferred only when
it is a native executable reporting a NEWER version. A turn never downloads:
with neither candidate the selection says so with a typed reason and the
transport raises :class:`~clio_agent.providers.copilot.errors.CopilotRuntimeMissingError`.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from clio_agent.providers.components.client_binary import (
    ClientBinary,
    ClientSelection,
    _cached,
    is_native_executable,
    probe_version,
    version_key,
)
from clio_agent.providers.copilot.errors import (
    COPILOT_RUNTIME_MISSING_MESSAGE,
    CopilotRuntimeMissingError,
)
from clio_agent.runtime.progress import ProbeUnresponsiveError

#: Ambient credentials the runtime would otherwise pick up instead of CLIO's
#: GitHub sign-in, and the path override that would bypass the selection.
SCRUBBED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_CLI_PATH")


def _wrapper_name() -> str:
    return "copilot-runtime.exe" if sys.platform == "win32" else "copilot-runtime"


def staged_runtime_path() -> str | None:
    """The staged runtime wrapper when the pinned bundle is complete, or ``None``.

    ``python -m copilot download-runtime`` stages the wrapper, ``runtime.node`` and the
    assets marker but not the ``copilot`` alias ``get_cached_cli_path`` also requires,
    so completeness is the SDK's own bundle rule for the wrapper it spawns.
    """
    try:
        from copilot._cli_download import (  # noqa: PLC0415
            _runtime_bundle_is_complete,
            get_cache_dir,
        )
        from copilot._cli_version import CLI_VERSION, get_runtime_platform  # noqa: PLC0415
    except ImportError:
        return None
    if not CLI_VERSION:
        return None
    try:
        pair_dir = get_cache_dir(CLI_VERSION) / "prebuilds" / get_runtime_platform()
    except RuntimeError:
        return None
    if not _runtime_bundle_is_complete(pair_dir, _wrapper_name()):
        return None
    return str(pair_dir / _wrapper_name())


def pinned_runtime_version() -> str:
    """The runtime release the installed SDK was built against (``""`` without the SDK)."""
    try:
        from copilot._cli_version import CLI_VERSION  # noqa: PLC0415
    except ImportError:
        return ""
    return str(CLI_VERSION or "")


def _installed_copilot() -> tuple[ClientBinary | None, str]:
    hit = shutil.which("copilot")
    if not hit:
        return None, "copilot_installed_not_found"
    candidate = Path(hit)
    if not is_native_executable(candidate):
        # npm's copilot.ps1/.cmd shim: the SDK spawns the path directly.
        return None, "copilot_installed_not_native"
    try:
        version = probe_version(str(candidate))
    except ProbeUnresponsiveError:
        return None, "copilot_installed_version_unresponsive"
    if not version:
        return None, "copilot_installed_version_unreadable"
    return ClientBinary(path=str(candidate), version=version, source="installed"), ""


def _select_copilot() -> ClientSelection:
    staged_path = staged_runtime_path()
    staged = (
        ClientBinary(path=staged_path, version=pinned_runtime_version(), source="bundled")
        if staged_path
        else None
    )
    installed, installed_reason = _installed_copilot()
    if installed is None:
        if staged is None:
            return ClientSelection(None, "copilot_no_runtime", None, None)
        return ClientSelection(staged, installed_reason, None, staged)
    if staged is None:
        return ClientSelection(installed, "copilot_runtime_not_staged", installed, None)
    installed_key, staged_key = version_key(installed.version), version_key(staged.version)
    if staged_key is None or (installed_key is not None and installed_key > staged_key):
        return ClientSelection(installed, "copilot_installed_newer", installed, staged)
    return ClientSelection(staged, "copilot_staged_not_older", installed, staged)


def copilot_client() -> ClientSelection:
    """The runtime the Copilot SDK transport spawns (cached until reset)."""
    return _cached("copilot", _select_copilot)


def runtime_path() -> str:
    """The selected runtime's path; raises when neither candidate exists."""
    selection = copilot_client()
    if selection.path is None:
        raise CopilotRuntimeMissingError(COPILOT_RUNTIME_MISSING_MESSAGE, detail=selection.reason)
    return selection.path


def runtime_env() -> dict[str, str]:
    """The child environment: ambient GitHub credentials removed, downloads and telemetry off."""
    env = {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}
    env["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
    env["COPILOT_GITHUB_TELEMETRY_DISABLED"] = "true"
    return env


__all__ = [
    "SCRUBBED_ENV",
    "copilot_client",
    "pinned_runtime_version",
    "runtime_env",
    "runtime_path",
    "staged_runtime_path",
]
