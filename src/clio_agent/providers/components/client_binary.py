"""Which provider CLI binary the SDK transports run: the user's installed one or the bundled one.

The Codex and Claude Code SDK transports each drive a CLI binary. Both SDK
wheels ship one (``openai-codex-cli-bin``'s ``codex``, ``claude-agent-sdk``'s
``_bundled/claude``), but the providers gate NEW MODELS by client version: the
ChatGPT backend hides a model from every Codex client older than the model's
``minimal_client_version``, so a bundled binary that lags the user's own install
hides models the user can already see in their terminal. This module is the ONE
answer to "which binary", used by the transports, model discovery and the
provider row's ``client`` fact:

* **Codex** (``codex_client``): the user's installed Codex CLI when one is found
  (PATH, or the npm/Homebrew/standalone install locations) -- that is what the
  picker promises ("Through the installed Codex"). The bundled binary only when
  none is installed.
* **Claude Code** (``claude_client``): the user's installed ``claude`` when it is
  NEWER than the bundled one, or when the wheel bundles none; otherwise the
  bundled one (it is the build the installed SDK was released against).

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
import platform
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

ClientSource = Literal["installed", "bundled"]

#: Bound on one ``--version`` probe so a hung binary cannot stall a provider check.
VERSION_PROBE_TIMEOUT_S = 5.0

_VERSION_PATTERN = re.compile(r"(\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z.]+)?)")

#: The npm platform package per target triple (``@openai/codex``'s own
#: ``bin/codex.js`` ``PLATFORM_PACKAGE_BY_TARGET``).
_CODEX_NPM_PLATFORM_PACKAGES: dict[str, str] = {
    "x86_64-unknown-linux-musl": "codex-linux-x64",
    "aarch64-unknown-linux-musl": "codex-linux-arm64",
    "x86_64-apple-darwin": "codex-darwin-x64",
    "aarch64-apple-darwin": "codex-darwin-arm64",
    "x86_64-pc-windows-msvc": "codex-win32-x64",
    "aarch64-pc-windows-msvc": "codex-win32-arm64",
}


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
    """Run ``<path> --version`` once and return the parsed version, ``""`` on failure."""
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv; the path is a discovered CLI
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_PROBE_TIMEOUT_S,
            check=False,
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


def _target_triple() -> str:
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    system = platform.system()
    if system == "Windows":
        return f"{arch}-pc-windows-msvc"
    if system == "Darwin":
        return f"{arch}-apple-darwin"
    return f"{arch}-unknown-linux-musl"


def _unique(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _codex_candidates() -> list[Path]:
    """Where a user-installed Codex CLI lives: PATH first, then the standard locations."""
    found: list[Path] = []
    for name in ("codex.exe", "codex") if os.name == "nt" else ("codex",):
        hit = shutil.which(name)
        if hit:
            found.append(Path(hit))
    home = Path.home()
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            found += [Path(appdata) / "npm" / "codex.cmd", Path(appdata) / "npm" / "codex"]
        found.append(home / ".local" / "bin" / "codex.exe")
    else:
        found += [
            home / ".npm-global" / "bin" / "codex",
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
            home / ".local" / "bin" / "codex",
            home / ".cargo" / "bin" / "codex",
        ]
    return _unique(found)


def _npm_vendor_codex(candidate: Path) -> Path | None:
    """The native binary behind an npm ``@openai/codex`` launcher (``codex.cmd``/``codex.js``)."""
    triple = _target_triple()
    platform_package = _CODEX_NPM_PLATFORM_PACKAGES.get(triple, "")
    exe_name = "codex.exe" if os.name == "nt" else "codex"
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    roots = [
        candidate.parent / "node_modules" / "@openai" / "codex",
        candidate.parent.parent / "lib" / "node_modules" / "@openai" / "codex",
    ]
    if resolved.name == "codex.js":
        roots.insert(0, resolved.parent.parent)
    for root in roots:
        bases = [
            root / "node_modules" / "@openai" / platform_package,
            root.parent / platform_package,
            root,
        ]
        for base in bases:
            exe = base / "vendor" / triple / "bin" / exe_name
            if is_native_executable(exe):
                return exe
    return None


def _installed_codex() -> tuple[ClientBinary | None, str]:
    """The user's installed Codex binary, or ``(None, typed reason)``."""
    unreadable = ""
    for candidate in _codex_candidates():
        if not candidate.exists():
            continue
        native = (
            candidate.resolve()
            if is_native_executable(candidate.resolve())
            else _npm_vendor_codex(candidate)
        )
        if native is None:
            continue
        version = probe_version(str(native))
        if version:
            return ClientBinary(path=str(native), version=version, source="installed"), ""
        unreadable = str(native)
    if unreadable:
        return None, "codex_installed_version_unreadable"
    return None, "codex_installed_not_found"


def _bundled_codex() -> ClientBinary | None:
    """``openai-codex-cli-bin``'s binary, versioned by its distribution metadata."""
    try:
        from codex_cli_bin import bundled_codex_path  # noqa: PLC0415

        path = bundled_codex_path()
    except (ImportError, FileNotFoundError, OSError):
        return None
    try:
        import importlib.metadata as metadata  # noqa: PLC0415

        version = metadata.version("openai-codex-cli-bin")
    except metadata.PackageNotFoundError:
        version = ""
    return ClientBinary(path=str(path), version=version, source="bundled")


def _select_codex() -> ClientSelection:
    installed, installed_reason = _installed_codex()
    bundled = _bundled_codex()
    if installed is not None:
        return ClientSelection(installed, "codex_installed_cli", installed, bundled)
    if bundled is not None:
        return ClientSelection(bundled, installed_reason, None, bundled)
    return ClientSelection(None, "codex_no_cli", None, None)


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
    unreadable = False
    for candidate in _claude_candidates():
        if not candidate.is_file():
            continue
        if os.name == "nt" and not is_native_executable(candidate):
            continue  # npm's claude.cmd shim: the SDK refuses to spawn it
        version = probe_version(str(candidate))
        if version:
            return ClientBinary(path=str(candidate), version=version, source="installed"), ""
        unreadable = True
    return None, (
        "claude_installed_version_unreadable" if unreadable else "claude_installed_not_found"
    )


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
            path=str(bundled_path), version=probe_version(str(bundled_path)), source="bundled"
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


def codex_client() -> ClientSelection:
    """The Codex binary the SDK half runs (cached until :func:`reset_client_cache`)."""
    return _cached("codex", _select_codex)


def claude_client() -> ClientSelection:
    """The ``claude`` binary the Claude Code SDK transport runs (cached until reset)."""
    return _cached("claude_code", _select_claude)


def provider_client(provider_kind: str) -> ClientSelection | None:
    """The selection for a provider kind that runs a CLI, else ``None``."""
    if provider_kind == "codex":
        return codex_client()
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
    "codex_client",
    "is_native_executable",
    "parse_version",
    "probe_version",
    "provider_client",
    "provider_client_fact",
    "reset_client_cache",
    "version_key",
]
