"""Wait for a started server to answer, and read what it has in force once it does.

Kept apart from :mod:`clio_agent.gact.infrastructure.runtime` so the runtime
stays an orchestrator: this module owns the two model-server specific steps
between "the container started" and "the operation succeeded".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from clio_agent.gact.infrastructure.container_runtime import parse_runtime_name
from clio_agent.gact.infrastructure.effective_parameters import observe_effective
from clio_agent.gact.infrastructure.model_runtimes import ENGINES, RUNTIME_FIELD
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    EffectiveParameter,
    ServiceRecord,
)
from clio_agent.gact.infrastructure.plan import Readiness
from clio_agent.gact.infrastructure.server_access import request_headers
from clio_agent.gact.infrastructure.server_parameters import PARAMETER_PREFIX

Execute = Callable[[CommandSpec], Awaitable[CommandResult]]
Progress = Callable[[str], None]

#: Seconds between health polls. Not a deadline: there is none.
POLL_INTERVAL_SECONDS = 2.0


class ServerExitedError(RuntimeError):
    """The server process ended before it ever answered its health check."""


async def wait_until_ready(
    readiness: Readiness,
    execute: Execute,
    progress: Progress,
    *,
    interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Poll the server's health until it answers; fail at once if it exits.

    There is no fixed deadline -- a large model on a CPU node can take many
    minutes to load. The wait ends when the server answers, when its process
    is gone (reported with its last log lines), or when the operation is
    cancelled by the person.

    Raises:
        ServerExitedError: The server stopped before answering.
    """

    started = time.monotonic()
    while True:
        health = await execute(readiness.health)
        answer = (health.stdout + health.stderr).casefold()
        if "no_http_client" in answer:
            raise ServerExitedError(
                "no_http_client: the target has neither curl nor wget, so CLIO cannot "
                f"tell when {readiness.label} answers. Install one of them on the target."
            )
        if "ready" in answer:
            return
        alive = await execute(readiness.alive)
        if "running" not in (alive.stdout + alive.stderr).casefold():
            logs = await execute(readiness.logs)
            tail = (logs.stdout + logs.stderr).strip()
            raise ServerExitedError(
                f"{readiness.label} stopped before it answered."
                + (f" Last output:\n{tail}" if tail else "")
            )
        elapsed = int(time.monotonic() - started)
        progress(f"Waiting for {readiness.label} to load and answer ({elapsed}s)")
        await asyncio.sleep(interval)


async def observe_service(
    record: ServiceRecord,
    execute: Execute,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> list[EffectiveParameter]:
    """The effective server parameters of a running managed model server.

    Returns an empty list for services that are not model runtimes or have no
    connection yet (nothing can be observed).
    """

    spec = ENGINES.get(record.service_id)
    if spec is None or not record.connection_url or record.state != "running":
        return []
    base = record.connection_url.rstrip("/")
    # A keyed server answers CLIO's reads only with its deployment key.
    headers = request_headers(record)

    async def http_get(path: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=5.0, transport=http_transport) as client:
                response = await client.get(f"{base}{path}", headers=headers)
        except httpx.HTTPError:
            return None
        return response.text if response.status_code == 200 else None

    # The native Windows llama.cpp is a process, not a container: there is no
    # runtime to inspect, so its launch values are reported as requested.
    # Parsed once, here at the boundary: an unknown stored value is a typed
    # RuntimeUnavailableError (runtime_unknown), never passed on as a string.
    runtime = (
        None
        if record.variant_id == "native-windows-cpu"
        else parse_runtime_name(record.configuration.get(RUNTIME_FIELD, "") or "docker")
    )
    requested = {
        key[len(PARAMETER_PREFIX) :]: value
        for key, value in record.configuration.items()
        if key.startswith(PARAMETER_PREFIX) and value.strip()
    }
    variant = "cpu" if record.variant_id == "native-windows-cpu" else record.variant_id
    return await observe_effective(
        spec.engine,
        variant,
        runtime=runtime,
        container_name=spec.container_name,
        requested=requested,
        http_get=http_get,
        run=execute,
    )
