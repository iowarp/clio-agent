"""The one place CLIO decides which ``git`` it runs.

A fresh Mac has no git: ``/usr/bin/git`` is a placeholder that opens the "Install
Command Line Developer Tools" dialog, and a fresh Windows or Linux desktop may have
none at all. The desktop runtime therefore ships its own relocatable git
(``<runtime>/git``, built by ``install/build-bundled-git.sh`` and
``install/build-gact-runtime.ps1``) and every git call CLIO makes resolves through
:func:`resolve_git`, in this order:

1. ``CLIO_GIT``: an explicit executable a person or test chose;
2. a git on ``PATH``, only when it is a real one (never the macOS placeholder): the
   person's own git, with their credential helpers, proxies and configuration;
3. the bundled git inside the desktop runtime, for a machine with no git.

When none qualifies, :class:`GitUnavailableError` says what to install, instead of
CLIO running the placeholder and leaving the person facing an installer dialog.

Child processes that call git themselves (workspace shells, ``uvx git+...`` MCP
servers) get the bundled git on ``PATH`` through :func:`with_bundled_git` under the
same rule: only when their ``PATH`` has no real git.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

#: Environment variable naming an explicit git executable (highest priority).
GIT_OVERRIDE_ENV = "CLIO_GIT"

#: The bundled git's executable, relative to ``<runtime>/git`` (MinGit on Windows; a
#: ``RUNTIME_PREFIX`` build elsewhere, which finds its helpers relative to itself).
BUNDLED_GIT_RELATIVE = "cmd/git.exe" if os.name == "nt" else "libexec/git-core/git"

#: The macOS ``xcrun`` placeholder: it is git only once developer tools exist.
_MACOS_PLACEHOLDER = Path("/usr/bin/git")
_MACOS_DEVELOPER_DIRS = (
    Path("/Applications/Xcode.app/Contents/Developer"),
    Path("/Library/Developer/CommandLineTools"),
)
_XCODE_SELECT_LINK = Path("/var/db/xcode_select_link")

#: CA bundles a Linux distribution provides; the bundled git's statically linked curl
#: has no reliable compiled-in default across distributions.
_LINUX_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL, CentOS
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/pki/tls/cacert.pem",  # OpenELEC
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL 7+
    "/etc/ssl/cert.pem",  # Alpine, macOS
)

#: Variables a bundled git must not inherit: another git's ``GIT_EXEC_PATH`` (an IDE
#: or a git wrapper exports one) would send it to that git's helpers instead of its own.
BUNDLED_GIT_CLEARED_ENV = ("GIT_EXEC_PATH",)

GitSource = Literal["override", "bundled", "path"]


class GitUnavailableError(RuntimeError):
    """No usable git: the message says why and what to install."""


@dataclass(frozen=True)
class GitExecutable:
    """A resolved git and the environment it needs beyond the caller's."""

    path: Path
    source: GitSource
    extra_env: Mapping[str, str] = field(default_factory=dict)
    cleared_env: tuple[str, ...] = ()

    def command(self, *args: str) -> list[str]:
        """The argv running this git with ``args``."""
        return [str(self.path), *args]

    def env(self, base: Mapping[str, str] | None = None) -> dict[str, str] | None:
        """The child environment, or None to inherit unchanged when nothing changes.

        ``extra_env`` fills gaps (a caller's own value wins); ``cleared_env`` is removed.
        """
        source = os.environ if base is None else base
        if base is None and not self.extra_env and not any(k in source for k in self.cleared_env):
            return None
        merged = {k: v for k, v in source.items() if k not in self.cleared_env}
        for key, value in self.extra_env.items():
            merged.setdefault(key, value)
        return merged


def bundled_git_root(runtime: Path | None = None) -> Path | None:
    """The runtime's ``git`` folder when it holds a usable git, else None."""
    if runtime is None:
        from clio_agent.runtime.execution_environment import bundled_root  # noqa: PLC0415

        runtime = bundled_root()
    if runtime is None:
        return None
    root = runtime / "git"
    return root if (root / BUNDLED_GIT_RELATIVE).is_file() else None


def bundled_git(runtime: Path | None = None) -> Path | None:
    """The bundled git executable, else None."""
    root = bundled_git_root(runtime)
    return None if root is None else root / BUNDLED_GIT_RELATIVE


def _macos_developer_dirs(environ: Mapping[str, str]) -> list[Path]:
    dirs: list[Path] = []
    if environ.get("DEVELOPER_DIR"):
        dirs.append(Path(environ["DEVELOPER_DIR"]))
    try:
        dirs.append(Path(os.readlink(_XCODE_SELECT_LINK)))
    except OSError:
        pass  # no xcode-select choice recorded: the defaults below apply
    dirs.extend(_MACOS_DEVELOPER_DIRS)
    return dirs


def is_macos_placeholder(path: str | Path, environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``path`` is macOS's ``/usr/bin/git`` with no developer tools behind it.

    Decided from the filesystem alone: running the placeholder is what opens the
    installer dialog, so this never executes it.
    """
    if sys.platform != "darwin":
        return False
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    if resolved != _MACOS_PLACEHOLDER:
        return False
    environ = os.environ if environ is None else environ
    return not any(
        (developer / "usr/bin/git").is_file() for developer in _macos_developer_dirs(environ)
    )


def real_git_on_path(
    path: str | None = None, environ: Mapping[str, str] | None = None
) -> Path | None:
    """A git on ``path`` (default ``PATH``) that is a real git, else None."""
    environ = os.environ if environ is None else environ
    search = environ.get("PATH", "") if path is None else path
    found = shutil.which("git", path=search)
    if not found or is_macos_placeholder(found, environ):
        return None
    return Path(found)


def _linux_ca_bundle() -> str | None:
    for candidate in _LINUX_CA_BUNDLES:
        if Path(candidate).is_file():
            return candidate
    try:
        import certifi  # noqa: PLC0415 - shipped in the runtime; optional elsewhere
    except ImportError:
        return None
    return certifi.where()


def bundled_git_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Variables the bundled git needs on this platform that ``environ`` lacks.

    macOS links Apple's libcurl (system trust store) and Windows ships MinGit with its
    own CA bundle, so only Linux needs ``GIT_SSL_CAINFO`` named.
    """
    environ = os.environ if environ is None else environ
    if not sys.platform.startswith("linux") or environ.get("GIT_SSL_CAINFO"):
        return {}
    bundle = _linux_ca_bundle()
    return {"GIT_SSL_CAINFO": bundle} if bundle else {}


def _unavailable_message(environ: Mapping[str, str]) -> str:
    found = shutil.which("git", path=environ.get("PATH", ""))
    if found and is_macos_placeholder(found, environ):
        return (
            "git is not installed: /usr/bin/git is only macOS's placeholder for the "
            "Command Line Developer Tools. Use CLIO Desktop (it includes git), install "
            f"the tools with `xcode-select --install`, or set {GIT_OVERRIDE_ENV} to a git "
            "executable."
        )
    return (
        "git is not installed: no git on PATH and no bundled git in this CLIO. Install "
        f"git, use CLIO Desktop (it includes git), or set {GIT_OVERRIDE_ENV} to a git "
        "executable."
    )


def resolve_git(environ: Mapping[str, str] | None = None) -> GitExecutable:
    """The git CLIO runs: ``CLIO_GIT``, then a real PATH git, then the bundled git."""
    environ = os.environ if environ is None else environ
    override = environ.get(GIT_OVERRIDE_ENV, "").strip()
    if override:
        candidate = shutil.which(override, path=environ.get("PATH", ""))
        if candidate is None and Path(override).is_file():
            candidate = override
        if candidate is None:
            raise GitUnavailableError(f"{GIT_OVERRIDE_ENV}={override!r} is not an executable")
        return GitExecutable(Path(candidate), "override")
    on_path = real_git_on_path(environ=environ)
    if on_path is not None:
        return GitExecutable(on_path, "path")
    bundled = bundled_git()
    if bundled is not None:
        return GitExecutable(bundled, "bundled", bundled_git_env(environ), BUNDLED_GIT_CLEARED_ENV)
    raise GitUnavailableError(_unavailable_message(environ))


def run_git(
    args: Sequence[str], *, env: Mapping[str, str] | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` of the resolved git (raises :class:`GitUnavailableError`)."""
    git = resolve_git()
    return subprocess.run(  # noqa: S603 - argv list, no shell
        git.command(*args), env=git.env(env), **kwargs
    )


def with_bundled_git(environment: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Put the bundled git on ``environment``'s PATH when that PATH has no real git.

    For children that run ``git`` by name. Returns ``environment`` (updated in place);
    unchanged when there is no bundled git or the PATH already has a real git.
    """
    root = bundled_git_root()
    if root is None:
        return environment
    # Callers merge a partial ``environment`` over ``os.environ``: judge the merged one.
    inherited = {**os.environ, **environment}
    path = inherited.get("PATH", "")
    if real_git_on_path(path, inherited) is not None:
        return environment
    directory = str((root / BUNDLED_GIT_RELATIVE).parent)
    environment["PATH"] = os.pathsep.join(part for part in (directory, path) if part)
    for key in BUNDLED_GIT_CLEARED_ENV:
        environment.pop(key, None)
        if key in os.environ:
            # Popping cannot hide the process's own value from a merge; git ignores an
            # empty GIT_EXEC_PATH (exec-cmd.c uses it only when non-empty).
            environment[key] = ""
    for key, value in bundled_git_env({**os.environ, **environment}).items():
        environment.setdefault(key, value)
    return environment
