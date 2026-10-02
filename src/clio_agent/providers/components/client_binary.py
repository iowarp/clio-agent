"""Which ``claude`` binary the Claude Code SDK transport runs: the user's or the bundled one.

The Claude Code SDK transport drives a CLI binary. ``claude-agent-sdk`` ships one
(``_bundled/claude``), but the provider gates NEW MODELS by client version, so a
bundled binary that lags the user's own install hides models the user can already
see in their terminal. This module is the ONE answer to "which binary", used by the
transport, model discovery and the provider row's ``client`` fact
(``claude_client``): the user's installed ``claude`` when it is NEWER than the
bundled one, or when the wheel bundles none; otherwise the bundled one (it is the
build the installed SDK was released against). Codex runs no CLI binary (its one
transport is direct), so it has no ``client`` fact.

Every outcome carries a typed ``reason`` (no silent fallback): an installed
binary that exists but cannot report a version is recorded as such, not skipped
without a word. A selection is cached per process and dropped by
:func:`reset_client_cache` -- an explicit provider check and a component update
both call it, so a newly installed CLI is picked up without a restart.

Credentials are never touched here: only ``--version`` is ever run.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

from clio_agent.runtime.process_progress import ProbeUnresponsiveError

logger = logging.getLogger(__name__)

ClientSource = Literal["installed", "bundled"]

#: A ``--version`` probe answering within this costs nothing extra; past it the probe
#: is waited for only while the CLI keeps working (a hung binary cannot stall a check).
VERSION_PROBE_TIMEOUT_S = 5.0

_VERSION_PATTERN = re.compile(r"(\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z.]+)?)")


@dataclass(frozen=True)
class ClientBinary:
    """One runnable CLI binary and the version it reports."""

    path: str
    version: str
    source: ClientSource


@dataclass(frozen=True)
class ClientSelection:
    """The binary a transport runs, plus both candidates and why this one won.

    ``client`` is ``None`` only when neither an installed nor a bundled binary
    exists; ``reason`` is always a typed code.
    """

    client: ClientBinary | None
    reason: str
    installed: ClientBinary | None = None
    bundled: ClientBinary | None = None

    @property
    def path(self) -> str | None:
        """The selected binary's path, or ``None`` when there is none."""
        return self.client.path if self.client is not None else None

    def to_wire(self) -> dict[str, Any]:
        """The provider row's ``client`` fact (typed source + version)."""
        return {
            "source": self.client.source if self.client else None,
            "version": self.client.version if self.client else "",
            "path": self.client.path if self.client else "",
            "reason": self.reason,
            "installed_version": self.installed.version if self.installed else "",
            "bundled_version": self.bundled.version if self.bundled else "",
        }


_LOCK = threading.Lock()
_CACHE: dict[str, ClientSelection] = {}


def reset_client_cache() -> None:
    """Forget every cached selection (explicit provider check, component update, tests)."""
    with _LOCK:
        _CACHE.clear()


def parse_version(text: str) -> str:
    """Return the first ``X.Y.Z`` version in a ``--version`` reply, or ``""``."""
    match = _VERSION_PATTERN.search(text or "")
    return match.group(1) if match else ""


def version_key(version: str) -> Version | None:
    """A comparable version, or ``None`` for an unparsable string."""
    try:
        return Version(version)
    except InvalidVersion:
        return None


def probe_version(path: str) -> str:
    """Run ``<path> --version`` once and return the parsed version, ``""`` on failure.

    Past :data:`VERSION_PROBE_TIMEOUT_S` the probe is waited for while the CLI keeps
    working (:func:`~clio_agent.runtime.process_progress.run_probe`).

    Raises:
        ProbeUnresponsiveError: the CLI launched but did not answer and stopped working
            -- installed but slow or unresponsive, which callers report as such, never as
            "not installed" (#1577).
    """
    from clio_agent.runtime.process_progress import run_probe  # noqa: PLC0415

    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = run_probe(
            [path, "--version"],
            op=f"{path} --version",
            first_wait_s=VERSION_PROBE_TIMEOUT_S,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "client version probe failed reason=client_version_probe_failed path=%s error=%s",
            path,
            exc,
        )
        return ""
    if result.returncode != 0:
        logger.warning(
            "client version probe failed reason=client_version_probe_failed path=%s exit_code=%d",
            path,
            result.returncode,
        )
        return ""
    return parse_version(result.stdout or result.stderr or "")


def is_native_executable(path: Path) -> bool:
    """Whether ``path`` is a binary CreateProcess/exec can run directly (not a script shim)."""
    try:
        if not path.is_file():
            return False
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    if os.name == "nt":
        return path.suffix.lower() == ".exe" and magic[:2] == b"MZ"
    # ELF, Mach-O (both endiannesses, 32/64-bit) and universal binaries.
    return magic in {
        b"\x7fELF",
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe",
    }


def _unique(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _claude_candidates() -> list[Path]:
    """Mirror of the Agent SDK's own system-wide ``claude`` search (native binaries only on Windows)."""
    found: list[Path] = []
    home = Path.home()
    if os.name == "nt":
        for name in ("claude", "claude.exe"):
            hit = shutil.which(name)
            if hit:
                found.append(Path(hit))
        found.append(home / ".local" / "bin" / "claude.exe")
    else:
        hit = shutil.which("claude")
        if hit:
            found.append(Path(hit))
        found += [
            home / ".npm-global" / "bin" / "claude",
            Path("/usr/local/bin/claude"),
            home / ".local" / "bin" / "claude",
            home / "node_modules" / ".bin" / "claude",
            home / ".yarn" / "bin" / "claude",
            home / ".claude" / "local" / "claude",
        ]
    return _unique(found)


def _installed_claude() -> tuple[ClientBinary | None, str]:
    unreadable = unresponsive = False
    for candidate in _claude_candidates():
        if not candidate.is_file():
            continue
        if os.name == "nt" and not is_native_executable(candidate):
            continue  # npm's claude.cmd shim: the SDK refuses to spawn it
        try:
            version = probe_version(str(candidate))
        except ProbeUnresponsiveError:
            unresponsive = True  # installed but slow: typed, never "not found"
            continue
        if version:
            return ClientBinary(path=str(candidate), version=version, source="installed"), ""
        unreadable = True
    if unresponsive:
        return None, "claude_installed_version_unresponsive"
    return None, (
        "claude_installed_version_unreadable" if unreadable else "claude_installed_not_found"
    )


def _bundled_version(path: str) -> str:
    """The bundled CLI's version; empty when it is unresponsive (run_probe logged why)."""
    try:
        return probe_version(path)
    except ProbeUnresponsiveError:
        return ""  # the selection then prefers a binary with a known version


def bundled_claude_path() -> Path | None:
    """The Agent SDK's own bundled CLI (``<claude_agent_sdk>/_bundled/claude(.exe)``), if vendored."""
    try:
        import claude_agent_sdk  # noqa: PLC0415

        package_dir = Path(claude_agent_sdk.__file__).resolve().parent
    except (ImportError, TypeError, AttributeError, ValueError, OSError):
        return None
    bundled = package_dir / "_bundled" / ("claude.exe" if os.name == "nt" else "claude")
    return bundled if bundled.is_file() else None


def _select_claude() -> ClientSelection:
    bundled_path = bundled_claude_path()
    bundled = (
        ClientBinary(
            path=str(bundled_path), version=_bundled_version(str(bundled_path)), source="bundled"
        )
        if bundled_path is not None
        else None
    )
    installed, installed_reason = _installed_claude()
    if installed is None:
        if bundled is None:
            return ClientSelection(None, "claude_no_cli", None, None)
        return ClientSelection(bundled, installed_reason, None, bundled)
    if bundled is None:
        return ClientSelection(installed, "claude_bundled_missing", installed, None)
    installed_key, bundled_key = version_key(installed.version), version_key(bundled.version)
    if bundled_key is None or (installed_key is not None and installed_key > bundled_key):
        return ClientSelection(installed, "claude_installed_newer", installed, bundled)
    return ClientSelection(bundled, "claude_bundled_not_older", installed, bundled)


def _cached(key: str, select: Any) -> ClientSelection:
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    selection: ClientSelection = select()
    with _LOCK:
        _CACHE[key] = selection
    logger.info(
        "provider client selected provider=%s source=%s version=%s reason=%s path=%s",
        key,
        selection.client.source if selection.client else None,
        selection.client.version if selection.client else "",
        selection.reason,
        selection.path,
    )
    return selection


def claude_client() -> ClientSelection:
    """The ``claude`` binary the Claude Code SDK transport runs (cached until reset)."""
    return _cached("claude_code", _select_claude)


def provider_client(provider_kind: str) -> ClientSelection | None:
    """The selection for a provider kind that runs a CLI, else ``None``."""
    if provider_kind == "claude_code":
        return claude_client()
    return None


def provider_client_fact(provider_kind: str, *, refresh: bool) -> dict[str, Any] | None:
    """The provider row's ``client`` fact; an explicit check (``refresh``) re-selects first.

    Re-selecting on an explicit check picks up a CLI the user installed or
    upgraded since the last check, without a restart.
    """
    if refresh:
        reset_client_cache()
    selection = provider_client(provider_kind)
    return selection.to_wire() if selection is not None else None


__all__ = [
    "ClientBinary",
    "ClientSelection",
    "ClientSource",
    "VERSION_PROBE_TIMEOUT_S",
    "bundled_claude_path",
    "claude_client",
    "is_native_executable",
    "parse_version",
    "probe_version",
    "provider_client",
    "provider_client_fact",
    "reset_client_cache",
    "version_key",
]
