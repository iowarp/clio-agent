"""Where a managed deployment keeps its state on a target.

One directory per host and deployment name: cluster nodes share one home, so
the host is part of the path, and named instances (``clio-vllm-small``) each
get their own directory beside the engine's default one.
"""

from __future__ import annotations

import ntpath
import posixpath

from clio_agent.gact.infrastructure.models import InfrastructureTarget, TargetFacts


def service_directory(name: str, facts: TargetFacts, target: InfrastructureTarget | None) -> str:
    """The per-host directory a deployment named ``name`` keeps its state in."""
    windows = facts.os == "windows"
    module = ntpath if windows else posixpath
    if target and any(target.storage.model_dump().values()):
        from clio_agent.gact.infrastructure.storage import resolved_locations  # noqa: PLC0415

        locations = resolved_locations(target, facts)
        return module.join(locations.service_data, facts.hostname or facts.target_id, name)
    root = (target.install_root.strip() if target else "").rstrip("/\\")
    if not root:
        root = facts.agent_data_root
    if not root:
        if not facts.home:
            raise ValueError(
                "Could not determine the target's home directory; set an install location for this host."
            )
        root = (
            module.join(facts.home, "AppData", "Local", "clio-agent", "data")
            if windows
            else module.join(facts.home, "Library", "Application Support", "clio-agent", "data")
            if facts.os == "macos"
            else module.join(facts.home, ".local", "share", "clio-agent")
        )
    if not module.isabs(root):
        raise ValueError("The target's Agent data directory must be absolute")
    # Cluster nodes share one home: without the host in the path, a login node
    # and a compute node deploying the same engine would share one cache and
    # one SIF, and uninstalling on one would delete the other's.
    host = facts.hostname or facts.target_id
    return module.join(root, "services", host, name)
