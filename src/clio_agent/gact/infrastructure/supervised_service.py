"""Shared driver plan for definition-owned, durable native supervisors."""

from __future__ import annotations

import json
import posixpath
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from clio_agent.gact.infrastructure import (
    native_service_platform,
    node_service,
    process_group,
    reuse,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    OwnedResource,
    TargetFacts,
)
from clio_agent.gact.infrastructure.plan import DriverPlan, Readiness
from clio_agent.gact.infrastructure.service_observation import parse_observation

#: A service environment's Python on a remote or Windows host: a pinned, tested minor.
PINNED_PYTHON = ">=3.12,<3.13"


def runs_on_clio_python(facts: TargetFacts) -> bool:
    """A local Linux or macOS service builds its environment on CLIO's own interpreter.

    The bundled runtime's Python then serves it, and uv downloads no second one (on
    macOS that download runs ``install_name_tool``, a Command Line Tools stub).
    Remote hosts keep the pinned minor, and Windows keeps uv-managed Python.
    """
    return facts.target_id == "local" and facts.os in {"linux", "macos"}


def service_python(facts: TargetFacts) -> tuple[str, dict[str, str]]:
    """The project's ``requires-python`` and the manifest entries that pick the interpreter."""
    if runs_on_clio_python(facts):
        return ">=3.12", {"interpreter": "clio"}
    return PINNED_PYTHON, {}


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
    windows: bool = False,
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
    platform_source = Path(native_service_platform.__file__).read_text(encoding="utf-8")
    fresh = reuse.from_scratch(configuration)

    def command(verb: str, *, cleanup: bool = False) -> CommandSpec:
        body: dict[str, object] = {
            "root": directory,
            "owner": ownership,
            "action": verb,
            "operation_id": operation_id,
            "script": script,
            "platform_helper": platform_source,
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
            if verb == "start" and (owner := configuration.get(reuse.OWNER_PROCESS_KEY, "")):
                body["owner_process"] = int(owner)
            extra = {
                "api_key_variable": key_variable,
                "secret_env": dict(secret_env or {}),
                "runtime_files": dict(runtime_files or {}),
            }
            body.update({k: v for k, v in extra.items() if v and verb == "start"})
        elif verb == "status" and action == "start" and api_key:
            # Readiness proves the endpoint lists our model with the per-launch key.
            body["api_key"] = api_key
        # The controller and manifest travel through stdin. Windows' command-line
        # limit must not constrain a service definition or expose its credentials.
        bootstrap = (
            "import json,sys,types\n"
            "request=json.load(sys.stdin)\n"
            "platform=types.ModuleType('clio_native_platform')\n"
            "sys.modules[platform.__name__]=platform\n"
            "exec(request['platform_helper'], platform.__dict__)\n"
            "controller=types.ModuleType('clio_native_controller')\n"
            "exec(request['script'], controller.__dict__)\n"
            "try:\n"
            " result=controller.control(request)\n"
            " print(result['logs'] if 'logs' in result else controller.MARKER+json.dumps(result))\n"
            "except (OSError,ValueError,RuntimeError) as error:\n"
            " print(str(error), file=sys.stderr); sys.exit(1)\n"
        )
        return CommandSpec(
            program=sys.executable if windows else "python3",
            args=["-c", bootstrap],
            stdin=json.dumps(body),
            timeout_seconds=120,
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
