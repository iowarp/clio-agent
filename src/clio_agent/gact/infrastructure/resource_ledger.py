"""The ledger of what a managed deployment created on its target, and how to remove it.

No remote side effects outlive a deployment: every container, Apptainer
instance log, image and directory CLIO creates while deploying a service is
recorded in the service's ledger (``ServiceRecord.owned_resources``), and
uninstall -- or the teardown of a failed or cancelled deploy -- removes exactly
those, newest first. Things that already existed before the deploy (an image
the person had pulled, a directory they made) are never recorded, so they are
never removed.

Ownership is decided from what the target reports, not assumed:

* an image is owned when :func:`image_recorder` saw it absent before the pull;
* directories are owned from the ``CLIO_CREATED_DIR`` / ``CLIO_CREATED_PARENT``
  lines :func:`create_directory_command` prints for each directory ``mkdir -p``
  had to create -- and a reported path is accepted only when it IS the
  requested directory or one of its ancestors, so terminal-mangled output can
  never name some other directory;
* a container / instance is owned from the moment its run step was attempted
  (a failed ``docker run`` can still leave a created container behind).

Removal is verified: each removal command checks afterwards that the thing is
gone and fails otherwise, so "removed" is never inferred from an exit code
that also means "the daemon is down".
"""

from __future__ import annotations

import logging
import ntpath
import posixpath
from collections.abc import Callable

from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    OwnedResource,
    RuntimeName,
)
from clio_agent.gact.infrastructure.transport_text import logical_lines

logger = logging.getLogger(__name__)

CREATED_DIR_MARKER = "CLIO_CREATED_DIR"
CREATED_PARENT_MARKER = "CLIO_CREATED_PARENT"

# Removal order: running things first, then what they used.
_REMOVAL_ORDER: dict[str, int] = {
    "container": 0,
    "instance_logs": 1,
    "image": 2,
    "directory": 3,
    "parent_directory": 4,
}


StepRecorder = Callable[[CommandResult], list[OwnedResource]]


def image_recorder(runtime: RuntimeName, image: str) -> StepRecorder:
    """Record the image as owned when the presence check found it absent."""

    def record(result: CommandResult) -> list[OwnedResource]:
        if result.exit_code == 0 or runtime == "apptainer":
            # Apptainer's SIF lives inside a CLIO-owned directory, removed with it.
            return []
        return [OwnedResource(kind="image", ref=image, runtime=runtime)]

    return record


def container_recorder(runtime: RuntimeName, name: str, host: str = "") -> StepRecorder:
    """Record the container (and Apptainer's instance logs on ``host``) once it was run."""

    def record(result: CommandResult) -> list[OwnedResource]:
        del result
        rows = [OwnedResource(kind="container", ref=name, runtime=runtime)]
        if runtime == "apptainer" and host:
            rows.append(OwnedResource(kind="instance_logs", ref=f"{host}/{name}", runtime=runtime))
        return rows

    return record


def directory_recorder(requested: str, os_name: str = "linux") -> StepRecorder:
    """Record the directories the create step reported, if they are ``requested`` or its ancestors."""

    module = ntpath if os_name == "windows" else posixpath
    target = module.normpath(requested)
    ancestors: set[str] = set()
    current = target
    while True:
        parent = module.dirname(current)
        if not parent or parent == current:
            break
        ancestors.add(parent)
        current = parent

    def record(result: CommandResult) -> list[OwnedResource]:
        rows: list[OwnedResource] = []
        lines = logical_lines(result.stdout or "", (CREATED_DIR_MARKER, CREATED_PARENT_MARKER))
        for line in lines:
            for marker, kind, allowed in (
                (CREATED_DIR_MARKER, "directory", {target}),
                (CREATED_PARENT_MARKER, "parent_directory", ancestors),
            ):
                if not line.startswith(f"{marker} "):
                    continue
                path = module.normpath(line[len(marker) + 1 :].strip())
                if path in allowed:
                    rows.append(OwnedResource(kind=kind, ref=path))  # type: ignore[arg-type]
                else:
                    logger.warning(
                        "infrastructure ledger: reason=unexpected_created_path path=%r "
                        "requested=%r -- not recorded",
                        path,
                        requested,
                    )
        return rows

    return record


def create_directory_command(path: str, os_name: str) -> CommandSpec:
    """``mkdir -p`` that reports every directory it had to create, outermost first."""

    if os_name == "windows":
        return powershell.command(
            f"$p={powershell.literal(path)}; $made=@(); $c=$p; "
            "while ($c -and -not (Test-Path -LiteralPath $c)) "
            "{ $made=@($c)+$made; $c=Split-Path -Parent $c }; "
            "New-Item -ItemType Directory -Force -Path $p | Out-Null; "
            "foreach ($d in $made) { if ($d -eq $p) { "
            f"Write-Output ('{CREATED_DIR_MARKER} ' + $d) }} else {{ "
            f"Write-Output ('{CREATED_PARENT_MARKER} ' + $d) }} }}"
        )
    # Newline-separated list with globbing off: a path may contain spaces,
    # "|" or "*" and is still one directory.
    return CommandSpec(
        program="sh",
        args=[
            "-c",
            # The newline is made at run time: a literal one would cross a PTY.
            'set -f; p="$0"; made=""; c="$p"; nl=$(printf \'\\n_\'); nl=${nl%_}; '
            'while [ -n "$c" ] && [ "$c" != / ] && [ ! -e "$c" ]; do '
            'made="$c$nl$made"; c=$(dirname -- "$c"); done; '
            'mkdir -p -- "$p" || exit 1; '
            'printf "%s" "$made" | while IFS= read -r d; do [ -z "$d" ] && continue; '
            f'if [ "$d" = "$p" ]; then echo "{CREATED_DIR_MARKER} $d"; '
            f'else echo "{CREATED_PARENT_MARKER} $d"; fi; done; true',
            path,
        ],
    )


def merge(existing: list[OwnedResource], added: list[OwnedResource]) -> list[OwnedResource]:
    """Append newly created resources, ignoring ones already in the ledger."""

    seen = {(row.kind, row.ref) for row in existing}
    merged = list(existing)
    for row in added:
        if (row.kind, row.ref) not in seen:
            merged.append(row)
            seen.add((row.kind, row.ref))
    return merged


def _verified(os_name: str, remove: str, still_there: str, posix_args: list[str]) -> CommandSpec:
    """Run ``remove``, then fail if ``still_there`` succeeds (the thing survived)."""

    if os_name == "windows":
        return powershell.command(
            f"{remove} *> $null; {still_there} *> $null; "
            "if ($LASTEXITCODE -eq 0) { Write-Output 'still present'; exit 1 } else { exit 0 }"
        )
    return CommandSpec(
        program="sh",
        args=[
            "-c",
            f"{remove} >/dev/null 2>&1; if {still_there} >/dev/null 2>&1; "
            'then echo "still present: $0"; exit 1; fi; true',
            *posix_args,
        ],
        timeout_seconds=600,
    )


def remove_container(runtime: RuntimeName, name: str, os_name: str) -> CommandSpec:
    """Remove a CLIO container / instance and confirm it is gone."""

    if runtime == "apptainer":
        return _verified(
            os_name,
            'apptainer instance stop "$0"',
            'apptainer instance list "$0" 2>/dev/null | tail -n +2 | grep -q .',
            [name],
        )
    if os_name == "windows":
        quoted = powershell.literal(name)
        return _verified(
            os_name, f"{runtime} rm --force {quoted}", f"{runtime} container inspect {quoted}", []
        )
    return _verified(
        os_name, f'{runtime} rm --force "$0"', f'{runtime} container inspect "$0"', [name]
    )


def remove_image(runtime: RuntimeName, image: str, os_name: str) -> CommandSpec:
    """Remove an image CLIO pulled and confirm it is gone."""

    if os_name == "windows":
        quoted = powershell.literal(image)
        return _verified(
            os_name, f"{runtime} rmi {quoted}", f"{runtime} image inspect {quoted}", []
        )
    return _verified(os_name, f'{runtime} rmi "$0"', f'{runtime} image inspect "$0"', [image])


def _remove_directory(path: str, os_name: str) -> CommandSpec:
    if os_name == "windows":
        quoted = powershell.literal(path)
        return powershell.command(
            f"if (Test-Path -LiteralPath {quoted}) {{ Remove-Item -Recurse -Force -LiteralPath {quoted} }}; "
            f"if (Test-Path -LiteralPath {quoted}) {{ Write-Output 'still present'; exit 1 }}",
            timeout_seconds=600,
        )
    return CommandSpec(
        program="sh",
        args=[
            "-c",
            'rm -rf -- "$0"; if [ -e "$0" ]; then echo "still present: $0"; exit 1; fi',
            path,
        ],
        timeout_seconds=600,
    )


def _remove_empty_directory(path: str, os_name: str) -> CommandSpec:
    """Remove a parent directory CLIO created, only if nothing else is in it."""

    if os_name == "windows":
        quoted = powershell.literal(path)
        return powershell.command(
            f"if ((Test-Path -LiteralPath {quoted}) -and "
            f"-not (Get-ChildItem -Force -LiteralPath {quoted})) "
            f"{{ Remove-Item -Force -LiteralPath {quoted} }}"
        )
    return CommandSpec(program="sh", args=["-c", 'rmdir -- "$0" 2>/dev/null; true', path])


def _remove_instance_logs(ref: str) -> CommandSpec:
    host, _, name = ref.partition("/")
    return CommandSpec(
        program="sh",
        args=[
            "-c",
            'rm -f -- "$HOME/.apptainer/instances/logs/$0/$USER/$1.out" '
            '"$HOME/.apptainer/instances/logs/$0/$USER/$1.err"',
            host,
            name,
        ],
    )


def _safe_directory(path: str, os_name: str) -> bool:
    module = ntpath if os_name == "windows" else posixpath
    normalized = module.normpath(path)
    return module.isabs(normalized) and normalized.count(module.sep) >= 2 and ".." not in path


def removal_commands(owned: list[OwnedResource], os_name: str) -> list[CommandSpec]:
    """Commands that remove every owned resource, running things first, newest first.

    A recorded directory that is not a dedicated absolute path is never
    removed (a corrupted ledger never turns into ``rm -rf /``); it is skipped
    with a typed warning so the rest of the ledger can still be removed.
    """

    ordered = sorted(enumerate(owned), key=lambda item: (_REMOVAL_ORDER[item[1].kind], -item[0]))
    commands: list[CommandSpec] = []
    for _, row in ordered:
        if row.kind == "container" and row.runtime:
            commands.append(remove_container(row.runtime, row.ref, os_name))
        elif row.kind == "instance_logs" and "/" in row.ref:
            commands.append(_remove_instance_logs(row.ref))
        elif row.kind == "image" and row.runtime:
            commands.append(remove_image(row.runtime, row.ref, os_name))
        elif row.kind in {"directory", "parent_directory"}:
            if not _safe_directory(row.ref, os_name):
                logger.warning(
                    "infrastructure ledger: reason=unsafe_ledger_path path=%r -- not removed",
                    row.ref,
                )
                continue
            remove = _remove_directory if row.kind == "directory" else _remove_empty_directory
            commands.append(remove(row.ref, os_name))
    return commands
