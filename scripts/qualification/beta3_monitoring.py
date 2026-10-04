"""Exercise production monitoring drivers on an owned homelab path, without inference."""

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
from clio_agent.gact.infrastructure.service_observation import parse_observation
from clio_agent.gact.infrastructure.service_readiness import wait_until_ready


async def qualify(service: str, output: Path, image: str) -> None:
    """Inspect, install, serve, write/read back, restart and remove only owned resources."""
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
    configuration = {
        "port": "18038" if service == "flowcept" else "18380",
        "container_runtime": "docker",
    }
    if service == "flowcept":
        configuration.update(redis_port="16389", mongo_port="37027")
    elif image:
        configuration["server_image"] = image
    evidence: dict[str, object] = {"service": service, "facts": facts.model_dump()}

    async def action(verb: str) -> dict[str, object]:
        nonlocal configuration
        plan = build_driver_plan(
            service_id=service,
            action=verb,
            variant_id="managed",
            configuration=configuration,
            facts=facts,
            target=target,
        )
        configuration = plan.configuration
        outputs = []
        for spec in plan.commands:
            result = await execute(spec)
            if result.exit_code:
                raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])
            outputs.append(result.stdout)
        if plan.readiness:
            await asyncio.wait_for(
                wait_until_ready(
                    plan.readiness, execute, lambda message: print(message, flush=True), interval=3
                ),
                900,
            )
            for spec in plan.after_ready:
                result = await execute(spec)
                outputs.append(result.stdout)
        observed = parse_observation(outputs)
        if observed is None:
            raise RuntimeError("Missing monitoring observation")
        print(verb + ": " + observed.phase, flush=True)
        return observed.model_dump()

    try:
        evidence["installed"] = await action("install")
        assert not evidence["installed"]["serving"]
        evidence["started"] = await action("start")
        assert not evidence["started"]["provenance_ingesting"]
        evidence["verified"] = await action("verify")
        assert evidence["verified"]["provenance_ingesting"]
        evidence["stopped"] = await action("stop")
        assert not evidence["stopped"]["running"]
        evidence["restarted"] = await action("start")
        assert not evidence["restarted"]["provenance_ingesting"]
        evidence["reverified"] = await action("verify")
        assert evidence["reverified"]["provenance_ingesting"]
        evidence["removed"] = await action("uninstall")
        assert not evidence["removed"]["installed"]
        evidence["configuration"] = configuration
        evidence["inference_tested"] = False
        output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    except (RuntimeError, TimeoutError, AssertionError):
        plan = build_driver_plan(
            service_id=service,
            action="logs",
            variant_id="managed",
            configuration=configuration,
            facts=facts,
            target=target,
        )
        result = await execute(plan.commands[0])
        print(result.stdout[-6000:])
        raise
    finally:
        await action("stop")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=["flowcept", "cmf"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server-image", default="")
    args = parser.parse_args()
    asyncio.run(qualify(args.service, args.output, args.server_image))
