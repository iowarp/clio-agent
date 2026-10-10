"""Shared driver plan for definition-owned, durable native supervisors."""

from __future__ import annotations

import json
import posixpath
from pathlib import Path
from typing import Any
from uuid import uuid4

from clio_agent.gact.infrastructure import node_service, process_group, reuse
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
    key_variable: str = "",
    secret_env: dict[str, str] | None = None,
    runtime_files: dict[str, str] | None = None,
) -> DriverPlan:
    """Keep lifecycle, readiness, ownership and failure cleanup identical across drivers.

    A start hands the server ``api_key`` (as ``key_variable``, default
    ``VLLM_API_KEY``) and any further ``secret_env`` through its environment,
    and writes ``runtime_files`` (non-secret, per-launch) into the service
    directory: neither changes the installed configuration's revision.
    """
    script = Path(node_service.__file__).read_text(encoding="utf-8")
    operation_id = str(uuid4())
    # Outside the manifest (whose digest is the configuration revision): the
    # shared reuse helper beside the worker, and this operation's bypass of it.
    helper = reuse.source()
    fresh = reuse.from_scratch(configuration)

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
            body.update(
                manifest=manifest,
                script=script,
                reuse_helper=helper,
                process_group_helper=process_group.source(),
            )
            if verb == "install" and fresh:
                body["from_scratch"] = True
            if verb == "start" and api_key:
                body["api_key"] = api_key
            extra = {
                "api_key_variable": key_variable,
                "secret_env": dict(secret_env or {}),
                "runtime_files": dict(runtime_files or {}),
            }
            body.update({k: v for k, v in extra.items() if v and verb == "start"})
        elif verb == "status" and action == "start" and api_key:
            # Readiness proves the endpoint lists our model with the per-launch key.
            body["api_key"] = api_key
        return CommandSpec(
            program="python3", args=["-c", script], stdin=json.dumps(body), timeout_seconds=120
        )

    def record(result: CommandResult) -> list[OwnedResource]:
        observed = parse_observation([result.stdout])
        return [OwnedResource(kind="directory", ref=directory)] if observed else []

    status = command("status")
    logs = posixpath.join(directory, "logs", "install.log" if action != "start" else "server.log")
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
            log_path=logs,
        )
        if action in {"install", "reinstall", "start"}
        else None,
        after_ready=(status,) if action in {"install", "reinstall", "start"} else (),
        retain_record=action != "delete_data",
        failure_cleanup=(command("stop", cleanup=True),)
        if action in {"install", "reinstall", "start"}
        else (),
    )
