"""Probe which container runtimes a target can actually use, and as whom.

The remote probe appends machine lines to the existing capability line:

* ``rt|<name>|<installed 0/1>|<usable 0/1>|<version>|<detail>`` per runtime:
  usable means the runtime answered its own health command (``docker info``,
  ``podman info``, ``apptainer --version``); when it did not, ``detail`` is the
  last line the runtime printed (for example rootless Podman on a Slurm
  compute node: ``stat /run/user/1008: no such file or directory``);
* ``id|<uid>|<gid>|<home>`` -- the account commands run as.

The same facts are gathered locally by running the same health commands.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from clio_agent.gact.infrastructure.models import (
    ContainerRuntimeFact,
    RuntimeName,
    TargetIdentity,
)
from clio_agent.gact.infrastructure.transport_text import logical_lines

#: POSIX shell fragment appended to the remote capability probe.
POSIX_RUNTIME_PROBE = (
    # A hung runtime (rootless Podman without its user runtime directory) must
    # not take the whole probe down: each health command gets its own bound.
    't=""; command -v timeout >/dev/null 2>&1 && t="timeout 20"; '
    "for rt in docker podman apptainer; do "
    'if command -v "$rt" >/dev/null 2>&1; then '
    'case "$rt" in '
    "docker) out=$($t docker info --format "
    "'{{.ServerVersion}}{{range .SecurityOptions}} {{.}}{{end}}' 2>&1) ;; "
    "podman) out=$($t podman info --format '{{.Version.Version}}' 2>&1) ;; "
    "apptainer) out=$($t apptainer --version 2>&1) ;; "
    "esac; st=$?; "
    "clean=$(printf '%s' \"$out\" | tr -d '|\\r' | grep -v '^[[:space:]]*$'); "
    'if [ "$st" -eq 0 ]; then '
    'printf \'rt|%s|1|1|%s|\\n\' "$rt" "$(printf \'%s\' "$clean" | head -n1 | cut -c1-80)"; '
    'else printf \'rt|%s|1|0||%s\\n\' "$rt" "$(printf \'%s\' "$clean" | tail -n1 | cut -c1-240)"; fi; '
    "else printf 'rt|%s|0|0||\\n' \"$rt\"; fi; done; "
    'printf \'id|%s|%s|%s|%s|\\n\' "$(id -u)" "$(id -g)" '
    '"$(hostname -s 2>/dev/null || hostname)" "$HOME"'
)

_HEALTH: dict[RuntimeName, list[str]] = {
    "docker": ["info", "--format", "{{.ServerVersion}}{{range .SecurityOptions}} {{.}}{{end}}"],
    "podman": ["info", "--format", "{{.Version.Version}}"],
    "apptainer": ["--version"],
}


def _version(name: RuntimeName, text: str) -> str:
    value = text.strip()
    if name == "apptainer" and value.casefold().startswith("apptainer version"):
        value = value.split()[-1]
    return value.split()[0] if value else ""


def _rootless(name: RuntimeName, text: str) -> bool:
    return name == "docker" and "rootless" in text


def parse_runtime_lines(stdout: str) -> tuple[list[ContainerRuntimeFact], TargetIdentity, str]:
    """Runtimes, identity and home of a remote probe (see :func:`parse_probe`)."""

    runtimes, identity, home, _ = parse_probe(stdout)
    return runtimes, identity, home


def parse_probe(stdout: str) -> tuple[list[ContainerRuntimeFact], TargetIdentity, str, str]:
    """Read the ``rt|`` and ``id|`` lines of a remote probe.

    Returns:
        The runtime facts (empty when the probe predates runtime lines), the
        target identity, the home directory and the short hostname.
    """

    runtimes: list[ContainerRuntimeFact] = []
    identity = TargetIdentity()
    home = ""
    hostname = ""
    for line in logical_lines(stdout, ("rt|", "id|")):
        fields = line.split("|")
        if fields[0] == "rt" and len(fields) >= 6 and fields[1] in _HEALTH:
            name: RuntimeName = fields[1]  # type: ignore[assignment]
            installed, usable = fields[2] == "1", fields[3] == "1"
            runtimes.append(
                ContainerRuntimeFact(
                    name=name,
                    installed=installed,
                    usable=usable,
                    version=_version(name, fields[4]) if usable else "",
                    rootless=usable and _rootless(name, fields[4]),
                    reason=None if usable else ("unusable" if installed else "not_installed"),
                    detail="" if usable else "|".join(fields[5:]).strip(),
                )
            )
        elif fields[0] == "id" and len(fields) >= 4:
            try:
                identity = TargetIdentity(uid=int(fields[1]), gid=int(fields[2]))
            except ValueError:
                identity = TargetIdentity()
            if len(fields) >= 6:
                # id|uid|gid|host|home| -- the trailing "|" ends the line, so
                # prompt residue the terminal appends never lands in home.
                hostname, home = fields[3].strip(), fields[4].strip()
            else:
                home = "|".join(fields[3:]).strip()
    return runtimes, identity, home, hostname


Resolver = Callable[[str], str | None]


def local_runtime_facts(resolve: Resolver) -> list[ContainerRuntimeFact]:
    """Probe the runtimes on the computer this CLIO runs on.

    Args:
        resolve: Executable resolution (the probe's Windows-aware resolver).
    """

    facts: list[ContainerRuntimeFact] = []
    for name, args in _HEALTH.items():
        executable = resolve(name)
        if executable is None:
            facts.append(ContainerRuntimeFact(name=name, reason="not_installed"))
            continue
        try:
            completed = subprocess.run(
                [executable, *args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            facts.append(
                ContainerRuntimeFact(name=name, installed=True, reason="unusable", detail=str(exc))
            )
            continue
        output = [row for row in (completed.stdout + completed.stderr).splitlines() if row.strip()]
        if completed.returncode == 0:
            first = output[0] if output else ""
            facts.append(
                ContainerRuntimeFact(
                    name=name,
                    installed=True,
                    usable=True,
                    version=_version(name, first),
                    rootless=_rootless(name, first),
                )
            )
        else:
            facts.append(
                ContainerRuntimeFact(
                    name=name,
                    installed=True,
                    reason="unusable",
                    detail=(
                        output[-1].strip() if output else f"exit status {completed.returncode}"
                    )[:240],
                )
            )
    return facts


def local_identity() -> tuple[TargetIdentity, str]:
    """The account and home directory of this CLIO's process."""

    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    identity = TargetIdentity(uid=getuid(), gid=getgid()) if getuid and getgid else TargetIdentity()
    return identity, str(Path.home())
