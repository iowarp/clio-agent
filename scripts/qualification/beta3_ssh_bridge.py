"""Exercise the real infrastructure transport through OpenSSH in a bounded local preview.

This qualification client executes real commands on the selected SSH profile. It does not
emulate a host, provision scheduler allocations, or replace the shipped Desktop bridge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.typing import Subprotocol


async def bridge(endpoint: str, target: str, profile: str, duration: int) -> None:
    """Forward CLIO-generated commands to an explicitly chosen test host until the deadline."""
    parsed = urlparse(endpoint)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("This qualification bridge is restricted to a local preview")
    uri = (
        endpoint.replace("http", "ws", 1).rstrip("/")
        + f"/v1/infrastructure/targets/{target}/transport"
    )
    protocols = [Subprotocol("clio.infrastructure.v2")]
    if token := os.environ.get("CLIO_QUALIFICATION_BEARER"):
        protocols.append(Subprotocol("clio-bearer." + token))
    async with websockets.connect(uri, subprotocols=protocols, max_size=4 * 1024 * 1024) as socket:

        async def execute(message: dict[str, Any]) -> None:
            command = message["command"]
            if command.get("scope", "target") != "target":
                raise ValueError("Controller commands are not supported by this test bridge")
            process = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                profile,
                shlex.join([command["program"], *command.get("args", [])]),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(command.get("stdin", "").encode()),
                    command["timeout_seconds"],
                )
                result = {
                    "exit_code": process.returncode,
                    "stdout": stdout.decode(errors="replace"),
                    "stderr": stderr.decode(errors="replace"),
                }
            except TimeoutError:
                process.kill()
                await process.wait()
                result = {
                    "exit_code": 124,
                    "stdout": "",
                    "stderr": "SSH command exceeded its deadline",
                }
            await socket.send(
                json.dumps(
                    {"type": "exec_result", "request_id": message["request_id"], "result": result}
                )
            )

        async with asyncio.TaskGroup() as group:
            async with asyncio.timeout(duration):
                async for raw in socket:
                    message = json.loads(raw)
                    if message["type"] == "attached":
                        print(
                            f"Attached qualification bridge for {target} through {profile}",
                            flush=True,
                        )
                    elif message["type"] == "exec":
                        group.create_task(execute(message))
                    else:
                        raise ValueError(
                            "This qualification bridge supports command execution only"
                        )


def main() -> None:
    """Run a bounded, visibly identified test transport."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:18814")
    parser.add_argument("--target", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--duration", type=int, default=3600)
    args = parser.parse_args()
    asyncio.run(bridge(args.endpoint, args.target, args.profile, args.duration))


if __name__ == "__main__":
    main()
