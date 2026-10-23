"""Run the separately installed NDP SDK with private stdin on CLIO or its SSH target."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.storage.ndp_endpoint_models import EndpointConnection
from clio_agent.gact.storage.sftp import connect_profile, connect_route

MAX_WORKER_OUTPUT = 2 * 1024 * 1024


def run_sdk(
    connection: EndpointConnection, request: dict[str, Any], token: str | None, seconds: float
) -> dict[str, Any]:
    """Bound SDK execution; never put account or broker credentials in shell arguments."""
    runtime = connection.streaming_runtime
    if runtime is None:
        raise ValueError("Configure an installed streaming_v2 runtime for this Endpoint")
    if connection.authentication == "ndp" and not token:
        raise PermissionError("Connect the NDP account before opening this SDK runtime")
    source = Path(__file__).with_name("ndp_streaming_worker.py").read_text(encoding="utf-8")
    arguments = [runtime.python, "-c", source]
    payload = json.dumps(
        {
            **request,
            "endpoint_url": connection.url,
            "profile_path": runtime.profile_path,
            "token": token if connection.authentication == "ndp" else None,
            "runtime_seconds": max(1, seconds - 1),
        }
    ).encode()
    route = connection.ssh_route or connection.ssh_profile
    if route:
        client = connect_route(route) if isinstance(route, SshRoute) else connect_profile(route)
        channel = None
        try:
            stdin, stdout, _ = client.exec_command(shlex.join(arguments), timeout=seconds)
            channel = stdout.channel
            stdin.write(payload)
            stdin.flush()
            channel.shutdown_write()
            deadline = time.monotonic() + seconds
            data = bytearray()
            while True:
                if channel.recv_ready():
                    data.extend(channel.recv(65536))
                    if len(data) > MAX_WORKER_OUTPUT:
                        raise ValueError("SDK capture exceeded the 2 MiB response limit")
                if channel.recv_stderr_ready():
                    channel.recv_stderr(
                        65536
                    )  # Drain SDK diagnostics, which may contain private configuration.
                # Exit status can arrive before the remaining stdout packets. Only
                # EOF establishes that the complete bounded JSON crossed SSH.
                if (
                    channel.exit_status_ready()
                    and channel.eof_received
                    and not channel.recv_ready()
                ):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "NDP streaming_v2 runtime exceeded its finite capture budget"
                    )
                time.sleep(0.005)
            exit_code = channel.recv_exit_status()
            if exit_code == 124:
                raise TimeoutError("NDP streaming_v2 runtime exceeded its finite capture budget")
            if exit_code != 0:
                raise RuntimeError(
                    "NDP SDK runtime failed; verify its interpreter and private profile"
                )
        finally:
            if channel is not None:
                channel.close()
            client.close()
    else:
        try:
            completed = subprocess.run(
                arguments, input=payload, capture_output=True, timeout=seconds, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                "NDP streaming_v2 runtime exceeded its finite capture budget"
            ) from exc
        if completed.returncode == 124:
            raise TimeoutError("NDP streaming_v2 runtime exceeded its finite capture budget")
        if completed.returncode:
            raise RuntimeError("NDP SDK runtime failed; verify its interpreter and private profile")
        data = bytearray(completed.stdout)
        if len(data) > MAX_WORKER_OUTPUT:
            raise ValueError("SDK capture exceeded the 2 MiB response limit")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("The NDP SDK runtime returned an invalid response")
    if result.get("error"):
        message = "NDP streaming_v2: " + str(result.get("message", result["error"]))
        if result["error"] == "PermissionError":
            raise PermissionError(message)
        raise ValueError(message)
    return result
