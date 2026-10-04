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
import socket
from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.typing import Subprotocol


class PortForwards(AbstractAsyncContextManager):
    """Own real loopback SSH forwards for the lifetime of the qualification bridge."""

    def __init__(self, profile: str, options: tuple[str, ...]) -> None:
        self.profile = profile
        self.options = options
        self.processes: list[asyncio.subprocess.Process] = []
        self.bindings: dict[int, tuple[int, asyncio.subprocess.Process]] = {}

    async def open(self, message: dict[str, Any]) -> str:
        """Confirm a real local listener before reporting a forward as established."""
        if message["remote_host"] != "127.0.0.1":
            raise ValueError("Qualification forwards only support remote loopback services")
        port = int(message["remote_port"])
        preferred = int(message.get("local_port") or 0)
        existing = self.bindings.get(preferred)
        if existing and existing[1].returncode is None:
            if existing[0] == port:
                return f"http://127.0.0.1:{preferred}"
            # Keep the prior forward valid for the registry's other remote-port entry.
            preferred = 0
        with socket.socket() as reservation:
            try:
                reservation.bind(("127.0.0.1", preferred))
            except OSError:
                if not preferred:
                    raise
                reservation.bind(("127.0.0.1", 0))
            local_port = reservation.getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{port}",
            *self.options,
            self.profile,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.processes.append(process)
        self.bindings[local_port] = (port, process)
        async with asyncio.timeout(15):
            while process.returncode is None:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", local_port)
                    writer.close()
                    await writer.wait_closed()
                    return f"http://127.0.0.1:{local_port}"
                except OSError:
                    await asyncio.sleep(0.1)
        raise RuntimeError("OpenSSH failed to establish the qualification forward")

    async def __aexit__(self, *args: Any) -> None:
        for process in self.processes:
            if process.returncode is None:
                process.terminate()
                await process.wait()


async def bridge(
    endpoint: str, target: str, profile: str, duration: int, ssh_options: tuple[str, ...] = ()
) -> None:
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
    async with (
        websockets.connect(uri, subprotocols=protocols, max_size=4 * 1024 * 1024) as socket,
        PortForwards(profile, ssh_options) as forwards,
    ):

        async def execute(message: dict[str, Any]) -> None:
            command = message["command"]
            if command.get("scope", "target") != "target":
                raise ValueError("Controller commands are not supported by this test bridge")
            process = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                *ssh_options,
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
                    elif message["type"] == "forward":
                        try:
                            url = await forwards.open(message)
                            response = {"type": "forward_result", "local_url": url}
                        except (OSError, TimeoutError, RuntimeError, ValueError) as exc:
                            response = {"type": "forward_error", "error": str(exc)}
                        await socket.send(
                            json.dumps(
                                {
                                    **response,
                                    "request_id": message["request_id"],
                                }
                            )
                        )
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
    parser.add_argument("--jump")
    parser.add_argument("--identity-file")
    parser.add_argument("--known-hosts")
    args = parser.parse_args()
    options = []
    if args.jump:
        options.extend(["-J", args.jump])
    if args.identity_file:
        options.extend(["-i", args.identity_file])
    if args.known_hosts:
        options.extend(
            ["-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + args.known_hosts]
        )
    asyncio.run(bridge(args.endpoint, args.target, args.profile, args.duration, tuple(options)))


if __name__ == "__main__":
    main()
