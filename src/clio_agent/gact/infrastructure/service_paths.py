"""Where a managed deployment keeps its state on a target.

One directory per host and deployment name: cluster nodes share one home, so
the host is part of the path, and named instances (``clio-vllm-small``) each
get their own directory beside the engine's default one.
"""

from __future__ import annotations

import logging
import ntpath
import platform
import posixpath
from pathlib import Path, PurePath

from clio_agent.gact.infrastructure.models import InfrastructureTarget, TargetFacts

logger = logging.getLogger(__name__)


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


def this_host_counterpart(path: str, host: str | None = None, *, live_service: bool = False) -> str:
    """Re-root a path inside another host's deployment directory at this host's.

    Settings that point into a managed deployment (``…/services/<host>/<name>/…``,
    e.g. Flowcept's ``settings.yaml`` or vLLM's capture ``evidence/``) were
    written on the host that installed it. On a cluster with a shared home the
    next node installs the same deployment under its own host directory, and
    the saved path would still name the previous node's copy. When this host's
    counterpart exists it is the live one; otherwise ``path`` is returned
    unchanged (nothing installed here yet, or not a managed path at all).

    ``live_service`` is for settings that describe a running service (e.g.
    Flowcept's Redis/MongoDB endpoints and credentials). Managed services
    listen on the installing host's loopback, so another host's copy is never
    the live one: the path resolves to this host's location even when nothing
    is installed there yet, and the caller reports it missing instead of
    connecting with the previous node's settings.
    """
    if not path:
        return path
    host = host or platform.node().split(".")[0]
    parts = PurePath(path).parts
    for index in range(len(parts) - 3, -1, -1):
        if parts[index] != "services":
            continue
        if parts[index + 1] == host:
            return path
        candidate = Path(*parts[: index + 1], host, *parts[index + 2 :])
        if not candidate.exists():
            if live_service:
                logger.info(
                    "%s belongs to host %s; this host has no deployment at %s yet",
                    path,
                    parts[index + 1],
                    candidate,
                )
                return str(candidate)
            return path
        logger.info(
            "using this host's deployment path %s instead of %s (saved on host %s)",
            candidate,
            path,
            parts[index + 1],
        )
        return str(candidate)
    return path
