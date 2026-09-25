"""Pinned CLI resolution + quiet-environment constants for the ``claude_code`` SDK
transport (B11/B12/B15 of the Claude subscription-provider tuning pass).

Owner module (#775 no-accretion): kept out of :mod:`claude_code_options` /
:mod:`claude_code_sessions` (both at or near their file-size ratchet) so the
one-time CLI discovery + version probe has its own small, independently
testable home.

**B12 (pinned CLI).** The Agent SDK's own ``_find_cli`` (re-derived here, not
imported — it lives under ``claude_agent_sdk._internal`` and is not a public
API) prefers a CLI binary bundled inside the installed ``claude-agent-sdk``
wheel (``<package>/_bundled/claude(.exe)``) before falling back to a
system-wide ``PATH``/well-known-location search. Pinning ``cli_path`` to that
bundled binary removes the fallback search's filesystem probing from every
connect and guarantees every ``claude_code`` session on this machine runs the
SAME CLI build the installed SDK wheel shipped with — no drift from whatever
else happens to be first on ``PATH``. When no bundled binary exists for this
platform (a wheel variant that does not vendor one), :func:`resolve_cli_path`
returns ``None`` (typed, logged) and the SDK's own discovery runs unchanged --
never a hard failure over a missing optimization.

**B11 (quiet environment).** :data:`QUIET_ENV` is merged by the SDK with the
INHERITED process environment (``ClaudeAgentOptions.env`` documents "explicit
env always wins"; verified against the installed SDK's subprocess transport),
so passing just these two keys is sufficient -- CLIO does not need to also
copy the parent environment itself.

**B15 (large tool outputs).** ``max_buffer_size`` bounds a single stdout line
the CLI subprocess transport can buffer before raising. CLIO's bare-model
transport (B3: ``tools=[]``) never streams a tool result through this
channel, but a large assistant turn (a long code block, a big JSON payload in
a ``submit`` argument) is still one line of the CLI's ``stream-json`` output,
and the SDK's own default (1 MiB) is tight enough to have been observed
tripping on those. :data:`MAX_BUFFER_SIZE` raises it to a named, documented
constant rather than leaving the tight default in place.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "CLI_VERSION_PROBE_TIMEOUT_S",
    "MAX_BUFFER_SIZE",
    "QUIET_ENV",
    "claude_cli_version",
    "reset_runtime_cache_for_tests",
    "resolve_cli_path",
]

#: B11: skip telemetry, update checks, and other non-essential network calls at
#: CLI startup. Merged by the SDK with the inherited process environment --
#: these two keys are added, not a full environment replacement.
QUIET_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}

#: B15: 16 MiB, comfortably above any observed CLIO turn (a submit-tool JSON
#: payload or a long assistant answer serialized as one stream-json line) while
#: still bounding a genuinely runaway line. The SDK's own default is 1 MiB.
MAX_BUFFER_SIZE = 16 * 1024 * 1024

#: Bound the one-time ``--version`` probe so a hung/misbehaving pinned binary
#: can never stall session construction indefinitely.
CLI_VERSION_PROBE_TIMEOUT_S = 5.0

_LOCK = threading.Lock()
_CLI_PATH_CACHE: dict[str, str | None] = {}
_VERSION_CACHE: dict[str, str] = {}


def _bundled_cli_path() -> Path | None:
    """The Agent SDK's own bundled CLI path for this platform, if vendored.

    Mirrors ``claude_agent_sdk._internal.transport.subprocess_cli
    .SubprocessCLITransport._find_bundled_cli`` (private, not imported)
    rather than depending on it: the bundled binary always lives at
    ``<claude_agent_sdk package dir>/_bundled/claude(.exe)``.
    """
    try:
        import claude_agent_sdk  # noqa: PLC0415

        package_dir = Path(claude_agent_sdk.__file__).resolve().parent
    except (ImportError, TypeError, AttributeError, ValueError, OSError):
        # A missing/invalid ``__file__`` covers a test's fake ``claude_agent_sdk``
        # module (a bare ``ModuleType(...)`` has none) — never a hard failure,
        # just no bundled binary to pin.
        return None
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled = package_dir / "_bundled" / cli_name
    if bundled.is_file():
        return bundled
    return None


def resolve_cli_path() -> str | None:
    """Return the pinned, bundled ``claude`` CLI path (resolved + cached once).

    ``None`` when this platform's installed SDK wheel does not vendor a
    bundled binary -- callers pass that straight through as
    ``ClaudeAgentOptions.cli_path=None``, which restores the SDK's own
    system-wide discovery (never a hard failure over a missing pin).
    """
    with _LOCK:
        if "path" in _CLI_PATH_CACHE:
            return _CLI_PATH_CACHE["path"]
        resolved = _bundled_cli_path()
        path_str = str(resolved) if resolved is not None else None
        _CLI_PATH_CACHE["path"] = path_str
        if path_str is None:
            logger.info(
                "claude_code cli pin: reason=claude_code_cli_not_bundled "
                "platform=%s -- falling back to the SDK's own system-wide discovery",
                platform.system(),
            )
        else:
            logger.info("claude_code cli pin: reason=claude_code_cli_pinned path=%s", path_str)
        return path_str


def claude_cli_version(cli_path: str | None) -> str:
    """Return ``<cli_path> --version`` output, probed and cached once per path.

    Empty string (typed, logged) when ``cli_path`` is ``None`` or the probe
    fails/times out -- a version-recording failure must never block a
    connect, only leave the recorded version blank.
    """
    if not cli_path:
        return ""
    with _LOCK:
        cached = _VERSION_CACHE.get(cli_path)
        if cached is not None:
            return cached
    version = _probe_version(cli_path)
    with _LOCK:
        _VERSION_CACHE[cli_path] = version
    return version


def _probe_version(cli_path: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - cli_path is our own pinned/discovered binary
            [cli_path, "--version"],
            capture_output=True,
            text=True,
            timeout=CLI_VERSION_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "claude_code cli version probe failed: reason=claude_code_cli_version_probe_failed "
            "path=%s error=%s",
            cli_path,
            exc,
        )
        return ""
    if result.returncode != 0:
        logger.warning(
            "claude_code cli version probe failed: reason=claude_code_cli_version_probe_failed "
            "path=%s exit_code=%d stderr=%s",
            cli_path,
            result.returncode,
            (result.stderr or "").strip()[:200],
        )
        return ""
    return (result.stdout or "").strip()


def reset_runtime_cache_for_tests() -> None:
    """Drop the cached CLI path / version (test isolation)."""
    with _LOCK:
        _CLI_PATH_CACHE.clear()
        _VERSION_CACHE.clear()


def claude_code_runtime_info() -> dict[str, Any]:
    """Resolved CLI path + probed version, for capabilities/server-info surfaces.

    A thin, side-effect-free (beyond the usual one-time caching) accessor so a
    doctor/status surface can report exactly what every ``claude_code`` session
    on this machine is pinned to, without duplicating the resolution logic.
    """
    path = resolve_cli_path()
    return {"cli_path": path, "cli_version": claude_cli_version(path)}
