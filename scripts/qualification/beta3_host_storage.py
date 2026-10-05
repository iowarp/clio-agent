"""Exercise production storage inspection over OpenSSH without modifying the target.

Example: uv run python scripts/qualification/beta3_host_storage.py homelab / /data --required-gib 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
from typing import Any

from anyio.to_thread import run_sync

from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec, InfrastructureTarget
from clio_agent.gact.infrastructure.storage import StorageInspectionRequest, inspect_target_path


async def check(host: str, paths: list[str], required_bytes: int) -> list[dict[str, Any]]:
    """Run the same production probe through an explicit OpenSSH route."""
    if host.startswith("-") or any(char in host for char in "\n\r\0"):
        raise ValueError("Invalid OpenSSH host")
    target = InfrastructureTarget(id=host, label=host, kind="ssh", transport_state="connected")

    async def execute(spec: CommandSpec) -> CommandResult:
        def run() -> CommandResult:
            completed = subprocess.run(
                [
                    "ssh",
                    "-oBatchMode=yes",
                    "-oConnectTimeout=10",
                    host,
                    shlex.join([spec.program, *spec.args]),
                ],
                input=spec.stdin,
                text=True,
                capture_output=True,
                timeout=spec.timeout_seconds + 15,
                check=False,
            )
            return CommandResult(
                exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
            )

        return await run_sync(run)

    return [
        await inspect_target_path(
            target,
            StorageInspectionRequest(
                path=path,
                required_bytes=required_bytes,
            ),
            execute,
        )
        for path in paths
    ]


def main() -> None:
    """Print real host observations suitable for the beta-3 qualification receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--required-gib", type=int, default=20)
    args = parser.parse_args()
    print(
        json.dumps(asyncio.run(check(args.host, args.paths, args.required_gib * 1024**3)), indent=2)
    )


if __name__ == "__main__":
    main()
