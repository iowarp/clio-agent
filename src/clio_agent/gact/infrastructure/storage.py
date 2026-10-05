"""Resolve host-bound storage locations and inspect them on their owning machine."""

from __future__ import annotations

import json
import ntpath
import posixpath
from pathlib import Path
from typing import Any

from anyio.to_thread import run_sync
from clio_schemas.connected_resources import HostStorageLocations
from pydantic import BaseModel, ConfigDict, Field

from clio_agent.gact.infrastructure.models import CommandSpec, InfrastructureTarget, TargetFacts
from clio_agent.gact.infrastructure.probe import CommandExecutor
from clio_agent.gact.infrastructure.storage_probe import inspect_path


class StorageInspectionRequest(BaseModel):
    """A path on the explicitly selected target and its required free capacity."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    required_bytes: int = Field(default=0, ge=0)
    browse: bool = False


def resolved_locations(target: InfrastructureTarget, facts: TargetFacts) -> HostStorageLocations:
    """Resolve inherited locations using target facts, never the controller's home."""
    paths = target.storage
    module = ntpath if facts.os == "windows" else posixpath
    root = paths.root or target.install_root or facts.agent_data_root
    if not root:
        raise ValueError("The target's Agent data directory is unknown; select a storage root")
    if not module.isabs(root):
        raise ValueError("Storage root must be absolute on the selected host")
    return HostStorageLocations(
        root=root,
        models=paths.models or module.join(root, "models"),
        service_data=paths.service_data or module.join(root, "services"),
        captures=paths.captures or module.join(root, "captures"),
        temporary=paths.temporary or module.join(root, "tmp"),
    )


async def inspect_target_path(
    target: InfrastructureTarget,
    request: StorageInspectionRequest,
    execute: CommandExecutor,
) -> dict[str, Any]:
    """Inspect only the requested machine; disconnected remotes never fall back locally."""
    if target.kind == "direct":
        raise ValueError("Connection-only endpoints do not expose a filesystem")
    if target.kind == "local":
        result = await run_sync(lambda: inspect_path(request.path, browse=request.browse))
    else:
        if target.transport_state != "connected":
            raise ValueError("Connect the selected host before browsing its storage")
        script = Path(__file__).with_name("storage_probe.py").read_text(encoding="utf-8")
        result_command = await execute(
            CommandSpec(
                program="python" if target.ssh and target.ssh.platform == "windows" else "python3",
                args=["-c", script],
                stdin=json.dumps({"path": request.path, "browse": request.browse}),
                timeout_seconds=30,
            )
        )
        if result_command.exit_code:
            raise ValueError(
                result_command.stdout.strip()
                or result_command.stderr.strip()
                or "Host filesystem inspection failed"
            )
        try:
            result = json.loads(result_command.stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError("Host returned an invalid storage inspection") from exc
        if not isinstance(result, dict) or not isinstance(result.get("free_bytes"), int):
            raise ValueError("Host returned an invalid storage inspection")
    return {
        **result,
        "target_id": target.id,
        "host_label": target.label,
        "required_bytes": request.required_bytes,
        "capacity_ok": result["free_bytes"] >= request.required_bytes,
    }
