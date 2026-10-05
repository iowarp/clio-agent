"""Shared driver plan for definition-owned, durable native supervisors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from clio_agent.gact.infrastructure import node_service
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec, OwnedResource
from clio_agent.gact.infrastructure.plan import DriverPlan, Readiness
from clio_agent.gact.infrastructure.service_observation import parse_observation


def supervised_plan(
    action: str,
    *,
    directory: str,
    ownership: str,
    manifest: dict[str, Any],
    port: int,
    label: str,
    configuration: dict[str, str],
    api_key: str | None = None,
) -> DriverPlan:
    """Keep lifecycle, readiness, ownership and failure cleanup identical across drivers."""
    script = Path(node_service.__file__).read_text(encoding="utf-8")
    operation_id = str(uuid4())

    def command(verb: str, *, cleanup: bool = False) -> CommandSpec:
        body: dict[str, object] = {
            "root": directory,
            "owner": ownership,
            "action": verb,
            "operation_id": operation_id,
        }
        if cleanup:
            body["require_operation_id"] = operation_id
        if verb in {"install", "start"}:
            body.update(manifest=manifest, script=script)
            if verb == "start" and api_key:
                body["api_key"] = api_key
        return CommandSpec(
            program="python3", args=["-c", script], stdin=json.dumps(body), timeout_seconds=120
        )

    def record(result: CommandResult) -> list[OwnedResource]:
        observed = parse_observation([result.stdout])
        return [OwnedResource(kind="directory", ref=directory)] if observed else []

    status = command("status")
    commands = (
        [command("prepare"), command("install")]
        if action == "install"
        else [command("stop"), command("prepare"), command("install")]
        if action == "reinstall"
        else [command(action)]
    )
    return DriverPlan(
        tuple(commands),
        connection_port=port,
        configuration=configuration,
        recorders={len(commands) - 2: record} if action in {"install", "reinstall"} else {},
        readiness=Readiness(
            status,
            status,
            command("logs"),
            f"{label} installation" if action != "start" else label,
            capability="installed" if action != "start" else "serving",
        )
        if action in {"install", "reinstall", "start"}
        else None,
        after_ready=(status,) if action in {"install", "reinstall", "start"} else (),
        retain_record=action != "delete_data",
        failure_cleanup=(command("stop", cleanup=True),)
        if action in {"install", "reinstall", "start"}
        else (),
    )
