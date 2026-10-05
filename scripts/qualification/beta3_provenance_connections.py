"""Prepare owned homelab services and private local settings for connection UI acceptance."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
from pathlib import Path

from clio_schemas.connected_resources import HostStorageLocations

from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec, InfrastructureTarget
from clio_agent.gact.infrastructure.probe import probe_target
from clio_agent.gact.infrastructure.service_readiness import wait_until_ready


async def prepare(output: Path, action: str) -> None:
    """Reuse the qualification roots and retain their evidence on removal."""
    target = InfrastructureTarget(
        id="homelab-monitoring-qualification",
        label="Homelab qualification",
        kind="ssh",
        transport_state="connected",
        storage=HostStorageLocations(root="/data/clio-beta3-qualification/monitoring"),
    )

    async def execute(spec: CommandSpec) -> CommandResult:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ssh", "-o", "BatchMode=yes", "homelab", shlex.join([spec.program, *spec.args])],
            input=spec.stdin or None,
            text=True,
            capture_output=True,
            timeout=spec.timeout_seconds + 5,
        )
        return CommandResult(
            exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    facts = await probe_target(target, execute)
    output.mkdir(parents=True, exist_ok=True)
    receipts: dict[str, object] = {}
    for service in ("flowcept", "cmf"):
        root = f"/data/clio-beta3-qualification/monitoring/services/Server/{service}"
        configuration = {
            "port": "18038" if service == "flowcept" else "18380",
            "container_runtime": "docker",
        }
        if service == "flowcept":
            configuration.update(redis_port="16389", mongo_port="37027")
        else:
            image = await execute(
                CommandSpec(
                    program="docker", args=["inspect", "--format", "{{.Image}}", "clio-cmf-server"]
                )
            )
            if image.exit_code:
                raise RuntimeError("Cannot inspect the existing CMF image")
            configuration["server_image"] = image.stdout.strip()
        verbs = ("install", "start") if action == "start" else ("stop", "uninstall")
        for verb in verbs:
            plan = build_driver_plan(
                service_id=service,
                action=verb,
                variant_id="managed",
                configuration=configuration,
                facts=facts,
                target=target,
            )
            configuration = plan.configuration
            for command in plan.commands:
                result = await execute(command)
                if result.exit_code:
                    raise RuntimeError(f"{service} {verb} failed; inspect its owned service logs")
            if plan.readiness:
                await asyncio.wait_for(
                    wait_until_ready(
                        plan.readiness,
                        execute,
                        lambda message: print(message, flush=True),
                        interval=3,
                    ),
                    900,
                )
            print(f"{service}: {verb} complete", flush=True)
        receipts[service] = configuration
        if service == "flowcept" and action == "start":
            # Credentials travel only through SSH into a private settings file; never stdout.
            result = await execute(CommandSpec(program="cat", args=[root + "/settings.yaml"]))
            if result.exit_code:
                raise RuntimeError("Cannot read qualification settings")
            settings = json.loads(result.stdout)
            for entry in (settings["mq"], settings["kv_db"]):
                entry["uri"] = entry["uri"].replace(":16389/", ":19939/")
                entry["port"] = 19939
            mongo = settings["databases"]["mongodb"]
            mongo["uri"] = mongo["uri"].replace(":37027/", ":19940/")
            mongo["port"] = 19940
            settings["log"]["log_path"] = str(output / "flowcept-client.log")
            settings["web_server"]["dashboards_dir"] = str(output / "dashboards")
            path = output / "flowcept-settings.yaml"
            path.write_text(json.dumps(settings), encoding="utf-8")
            path.chmod(0o600)
    (output / "service-locations.json").write_text(json.dumps(receipts, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--action", choices=["start", "stop"], required=True)
    args = parser.parse_args()
    asyncio.run(prepare(args.output, args.action))
