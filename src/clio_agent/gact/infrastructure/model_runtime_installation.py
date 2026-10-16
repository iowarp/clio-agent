"""Install a pinned model-server runtime without loading weights or opening a port."""

from __future__ import annotations

import ntpath
import posixpath
from dataclasses import dataclass

from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.container_runtime import (
    image_present_command,
    image_reuse_check,
    pull_commands,
    remove_container_command,
)
from clio_agent.gact.infrastructure.models import (
    CommandSpec,
    InfrastructureTarget,
    OwnedResource,
    RuntimeName,
    TargetFacts,
)
from clio_agent.gact.infrastructure.node_service import MARKER
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.resource_ledger import (
    StepRecorder,
    create_directory_command,
    directory_recorder,
    image_recorder,
    removal_commands,
    shared_image_recorder,
)
from clio_agent.gact.infrastructure.reuse import from_scratch
from clio_agent.gact.infrastructure.service_paths import service_directory


@dataclass(frozen=True)
class RuntimeStorage:
    """Resolved existing runtime paths, shared by package installation and server launch."""

    cache: str
    temporary: str
    images: str
    image_store: str
    configuration: dict[str, str]


def runtime_storage(
    name: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None,
) -> RuntimeStorage:
    """Preserve the recorded paths before consulting host defaults."""
    module = ntpath if facts.os == "windows" else posixpath
    directory = configuration.get("storage.service_directory") or service_directory(
        name, facts, target
    )
    cache = configuration.get("storage.model_cache") or module.join(directory, "cache")
    temporary = configuration.get("storage.temporary") or module.join(directory, "tmp")
    image_store = module.join(module.dirname(directory.rstrip("/\\")), "apptainer-images")
    if target and any(target.storage.model_dump().values()):
        from clio_agent.gact.infrastructure.storage import resolved_locations  # noqa: PLC0415

        locations = resolved_locations(target, facts)
        cache = configuration.get("storage.model_cache") or module.join(
            locations.models, facts.hostname or facts.target_id, name
        )
        temporary = configuration.get("storage.temporary") or module.join(
            locations.temporary, facts.hostname or facts.target_id, name
        )
        image_store = module.join(locations.service_data, "apptainer-images")
    return RuntimeStorage(
        cache,
        temporary,
        module.join(directory, "images"),
        configuration.get("storage.image_store") or image_store,
        {
            "storage.service_directory": directory,
            "storage.model_cache": cache,
            "storage.temporary": temporary,
        },
    )


def llama_start_configuration(
    installed: dict[str, str], requested: dict[str, str]
) -> dict[str, str]:
    """Apply model/server edits on Start without relocating installed runtime storage."""
    merged = dict(installed)
    for key, value in requested.items():
        if key.startswith("storage.") or key == "container_runtime":
            if value and value != installed.get(key):
                raise ValueError(
                    "Changing installed runtime storage or container engine requires reconfiguration"
                )
        else:
            merged[key] = value  # Explicitly clearing the other model source is meaningful.
    return merged


def runtime_observation(windows: bool, *, serving: bool = False) -> CommandSpec:
    """Emit installation proof, or serving proof only after the readiness gate passes."""
    phase, active = ("running", "true") if serving else ("stopped", "false")
    body = (
        f'{{"phase":"{phase}","installed":true,"running":{active},"serving":{active},"observed_at":'
    )
    if windows:
        return powershell.command(
            "$stamp=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); Write-Output ("
            + powershell.literal(MARKER + body)
            + "+$stamp+'}')"
        )
    return CommandSpec(
        program="sh",
        args=[
            "-c",
            'printf \'%s%s}\\n\' "$1" "$(date +%s)"',
            "clio-install-receipt",
            MARKER + body,
        ],
    )


def container_installation_plan(
    *,
    runtime: RuntimeName,
    name: str,
    image: str,
    action: str,
    configuration: dict[str, str],
    os: str,
    cache_dir: str,
    temporary_dir: str,
    images_dir: str,
    image_store: str,
    owned: list[OwnedResource] | None,
) -> DriverPlan:
    """Prepare cached image/directories using existing ownership and reuse semantics."""
    module = ntpath if os == "windows" else posixpath
    commands: list[CommandSpec] = []
    recorders: dict[int, StepRecorder] = {}
    if action == "reinstall":
        commands.extend(
            removal_commands(
                [row for row in owned or [] if row.kind in {"container", "instance_logs"}], os
            )
        )
    commands.append(remove_container_command(runtime, name))
    for directory in [cache_dir, temporary_dir] + ([images_dir] if runtime == "apptainer" else []):
        recorders[len(commands)] = directory_recorder(directory, os)
        commands.append(create_directory_command(directory, os))
    present = len(commands)
    recorders[present] = image_recorder(runtime, image)
    commands.append(image_present_command(runtime, image, images_dir, name))
    commands.extend(
        pull_commands(
            runtime,
            image,
            images_dir,
            image_store,
            name,
            module.join(temporary_dir, "apptainer-tmp"),
            fresh=from_scratch(configuration),
        )
    )
    check = image_reuse_check(runtime, image, tuple(range(present + 1, len(commands))))
    if runtime == "apptainer":
        recorders[len(commands) - 1] = shared_image_recorder()
    # Presence before pull is a reuse probe; presence afterward is mandatory.
    commands.append(
        image_present_command(runtime, image, images_dir, name).model_copy(
            update={"allowed_exit_codes": [0]}
        )
    )
    commands.append(runtime_observation(os == "windows"))
    return DriverPlan(
        tuple(commands),
        recorders=recorders,
        configuration=configuration,
        reuse_checks={present: check} if check else {},
    )
