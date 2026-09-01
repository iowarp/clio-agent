"""Static entry-point resolution for a ``uv run`` stdio MCP launcher.

A declared stdio MCP server whose command is ``uv`` names TWO preconditions the
subprocess needs before it can speak MCP at all: the ``--project`` directory
(already stat'd by callers) and the ENTRY POINT that directory's virtual
environment must provide. This module answers the second one the same way the
first is answered — by looking at the filesystem, never by launching anything.

The live defect that motivates it (``sess_086cf23a960b``): a deployment set its
project directory to a real path whose ``.venv`` had been created but never
synced — a bare interpreter, no ``site-packages``. Every directory-level check
passed, and ``uv run --project <dir> --no-sync spotter-mcp`` then failed with
"program not found" on every attempt to start the server.

**Scope is deliberately narrow — the parser refuses to guess.**
:func:`parse_uv_run_launcher` models exactly one shape, ``uv run [flags]
<console-script> [args]``, and returns ``None`` (no opinion, caller keeps its
prior behavior) for everything else:

* a command that is not ``uv``, or a subcommand that is not ``run``;
* ``-m`` / ``--module`` / ``--script`` / ``-s`` / ``--gui-script``, which change
  what the first positional argument MEANS;
* a script path (contains a separator, or ends in ``.py``) rather than a
  console script name;
* no ``--project`` / ``--directory`` (the environment would then be resolved
  from the server process's cwd, which is not knowable here);
* ANY flag token this module does not have in :data:`_VALUE_FLAGS` or
  :data:`_BOOL_FLAGS`. An unknown flag might consume the next token, and
  mistaking a flag's VALUE for the entry point would manufacture a false
  refusal — the one failure mode a static gate must never have.

:attr:`UvRunLauncher.sync_disabled` reports whether ``--no-sync`` was declared.
It matters to callers because without it ``uv run`` PROVISIONS the environment
before exec: an absent or half-populated venv is then not a static precondition
failure at all, and refusing on one would block a working deployment.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "VENV_STATE_ENTRYPOINT_ABSENT",
    "VENV_STATE_MISSING",
    "VENV_STATE_UNSYNCED",
    "UvRunLauncher",
    "entrypoint_venv_state",
    "parse_uv_run_launcher",
]

#: The project declares no virtual environment at all: ``<project>/.venv`` is
#: not a directory, so a ``--no-sync`` run has nothing to execute from.
VENV_STATE_MISSING = "missing_venv"

#: The virtual environment exists but was never populated -- no (or empty)
#: ``site-packages``. This is the live ``sess_086cf23a960b`` topology: a bare
#: interpreter that satisfies every directory-level check and spawns nothing.
VENV_STATE_UNSYNCED = "unsynced"

#: The environment is populated, but neither a console script nor a matching
#: installed distribution provides this entry point.
VENV_STATE_ENTRYPOINT_ABSENT = "entrypoint_absent"

#: ``uv run`` options that CONSUME the following token. Anything here is
#: skipped together with its value when scanning for the first positional.
_VALUE_FLAGS = frozenset(
    {
        "--project",
        "--directory",
        "--package",
        "--python",
        "-p",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--extra",
        "--no-extra",
        "--group",
        "--no-group",
        "--only-group",
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "-f",
        "--index-strategy",
        "--keyring-provider",
        "--resolution",
        "--prerelease",
        "--fork-strategy",
        "--config-setting",
        "-C",
        "--config-settings-package",
        "--exclude-newer",
        "--exclude-newer-package",
        "--link-mode",
        "--no-binary-package",
        "--no-build-package",
        "--refresh-package",
        "--constraints",
        "--constraint",
        "-c",
        "--overrides",
        "--override",
        "--build-constraints",
        "-b",
        "--env-file",
        "--cache-dir",
        "--config-file",
        "--color",
        "--python-preference",
        "--torch-backend",
        "--allow-insecure-host",
        "--trusted-host",
    }
)

#: ``uv run`` options that stand alone. Kept explicit rather than inferred so an
#: option this module has never seen bails out instead of being guessed at.
_BOOL_FLAGS = frozenset(
    {
        "--no-sync",
        "--frozen",
        "--no-frozen",
        "--locked",
        "--no-locked",
        "--dev",
        "--no-dev",
        "--only-dev",
        "--group-all",
        "--all-groups",
        "--no-default-groups",
        "--all-extras",
        "--no-all-extras",
        "--all-packages",
        "--no-editable",
        "--exact",
        "--inexact",
        "--isolated",
        "--no-project",
        "--no-sources",
        "--no-install-project",
        "--no-install-workspace",
        "--refresh",
        "--no-refresh",
        "--upgrade",
        "-U",
        "--no-upgrade",
        "--reinstall",
        "--no-reinstall",
        "--compile-bytecode",
        "--no-compile-bytecode",
        "--no-binary",
        "--no-build",
        "--no-build-isolation",
        "--no-index",
        "--offline",
        "--no-offline",
        "--native-tls",
        "--no-native-tls",
        "--no-cache",
        "-n",
        "--no-config",
        "--no-progress",
        "--quiet",
        "-q",
        "--verbose",
        "-v",
        "--preview",
        "--no-preview",
        "--managed-python",
        "--no-managed-python",
        "--system",
        "--no-system",
        "--active",
        "--no-active",
        "--locked-check",
    }
)

#: Options after which the first positional is NOT a console script name.
_ENTRYPOINT_REDEFINING_FLAGS = frozenset({"-m", "--module", "--script", "-s", "--gui-script"})

#: Flags whose value is the project directory the environment belongs to.
_PROJECT_FLAGS = ("--project", "--directory")

#: PEP 503 normalization, used ONLY to compare an entry-point name against an
#: installed distribution's directory name.
_NORMALIZE = re.compile(r"[-_.]+")


@dataclass(frozen=True)
class UvRunLauncher:
    """One recognized ``uv run`` stdio launcher, decomposed for static checks.

    Attributes:
        project_dir: The ``--project`` / ``--directory`` value (expanded).
        entrypoint: The console script ``uv run`` would exec.
        sync_disabled: ``--no-sync`` was declared, so ``uv`` will NOT provision
            the environment before exec and its current contents are binding.
    """

    project_dir: str
    entrypoint: str
    sync_disabled: bool


def _is_uv(command: str) -> bool:
    """True when ``command`` invokes the ``uv`` binary (path- and suffix-tolerant)."""

    stem = Path(command.strip().strip('"')).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem == "uv"


def parse_uv_run_launcher(command: str, args: Sequence[str]) -> Optional[UvRunLauncher]:
    """Decompose a ``uv run --project <dir> [flags] <console-script>`` argv.

    Args:
        command: The launcher command from the normalized MCP spec.
        args: That spec's already env-expanded argument vector.

    Returns:
        The :class:`UvRunLauncher` when the argv matches the modeled shape
        exactly, else ``None`` — the module's way of saying "no opinion". See
        the module docstring for the full list of shapes it declines.
    """

    if not _is_uv(command):
        return None
    argv = [str(a) for a in args]
    if not argv or argv[0] != "run":
        return None

    project = ""
    sync_disabled = False
    entrypoint = ""
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            entrypoint = argv[index + 1] if index + 1 < len(argv) else ""
            break
        if token.startswith("-") and token != "-":
            flag, _, inline = token.partition("=")
            if flag in _ENTRYPOINT_REDEFINING_FLAGS:
                return None
            if flag in _VALUE_FLAGS:
                if inline:
                    value = inline
                    index += 1
                else:
                    value = argv[index + 1] if index + 1 < len(argv) else ""
                    index += 2
                if flag in _PROJECT_FLAGS:
                    project = value
                continue
            if flag in _BOOL_FLAGS and not inline:
                if flag == "--no-sync":
                    sync_disabled = True
                index += 1
                continue
            # An option this module does not model: it may or may not consume
            # the next token, so the entry point cannot be identified safely.
            return None
        entrypoint = token
        break

    if not project or not entrypoint:
        return None
    if entrypoint.endswith(".py") or "/" in entrypoint or "\\" in entrypoint:
        return None  # a script path, not a console script installed in the venv
    return UvRunLauncher(project_dir=project, entrypoint=entrypoint, sync_disabled=sync_disabled)


def _bin_dirs(venv: Path) -> Iterator[Path]:
    """Yield both venv script layouts (``Scripts`` and ``bin``).

    Probing both on every platform is intentional: a project directory may have
    been provisioned under a different OS (a mounted checkout, a container
    bind), and a check that only ever ADDS ways to resolve can never invent a
    refusal.
    """

    for name in ("Scripts", "bin"):
        candidate = venv / name
        if candidate.is_dir():
            yield candidate


def _site_packages(venv: Path) -> list[Path]:
    """Return every populated ``site-packages`` directory in ``venv``."""

    found: list[Path] = []
    windows_layout = venv / "Lib" / "site-packages"
    if windows_layout.is_dir():
        found.append(windows_layout)
    for lib in ("lib", "lib64"):
        parent = venv / lib
        if not parent.is_dir():
            continue
        try:
            children = sorted(parent.iterdir())
        except OSError:
            continue
        for child in children:
            candidate = child / "site-packages"
            if candidate.is_dir():
                found.append(candidate)
    return found


def _has_console_script(venv: Path, entrypoint: str) -> bool:
    """True when a venv script directory carries an executable for ``entrypoint``."""

    suffixes = ("", ".exe", ".cmd", ".bat", ".ps1") if os.name == "nt" else ("", ".exe")
    for bindir in _bin_dirs(venv):
        for suffix in suffixes:
            if (bindir / f"{entrypoint}{suffix}").exists():
                return True
    return False


def _has_distribution(sites: Sequence[Path], entrypoint: str) -> bool:
    """True when an installed distribution's name matches ``entrypoint``.

    A permissive fallback for an environment whose console scripts were not
    linked (an editable install, a relocated venv): matching the normalized
    distribution name can only make the check pass, never fail.
    """

    wanted = _NORMALIZE.sub("_", entrypoint.strip().lower())
    if not wanted:
        return False
    for site in sites:
        try:
            entries = list(site.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            for marker in (".dist-info", ".egg-info", ".egg-link"):
                if not name.endswith(marker):
                    continue
                stem = name[: -len(marker)].rsplit("-", 1)[0]
                if _NORMALIZE.sub("_", stem.lower()) == wanted:
                    return True
    return False


def entrypoint_venv_state(project_dir: str, entrypoint: str) -> str:
    """Statically classify whether ``project_dir``'s venv can exec ``entrypoint``.

    Args:
        project_dir: The launcher's ``--project`` value.
        entrypoint: The console script ``uv run`` would exec.

    Returns:
        ``""`` when the entry point resolves, otherwise one of
        :data:`VENV_STATE_MISSING`, :data:`VENV_STATE_UNSYNCED`, or
        :data:`VENV_STATE_ENTRYPOINT_ABSENT` — the actionable half of the
        caller's refusal, because each names a different operator fix.
    """

    venv = Path(project_dir).expanduser() / ".venv"
    if not venv.is_dir():
        return VENV_STATE_MISSING
    if _has_console_script(venv, entrypoint):
        return ""
    sites = _site_packages(venv)
    populated = []
    for site in sites:
        try:
            if any(site.iterdir()):
                populated.append(site)
        except OSError:
            continue
    if not populated:
        return VENV_STATE_UNSYNCED
    if _has_distribution(populated, entrypoint):
        return ""
    return VENV_STATE_ENTRYPOINT_ABSENT
