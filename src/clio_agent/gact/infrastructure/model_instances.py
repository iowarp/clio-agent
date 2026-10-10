"""Named instances of a managed model engine: several deployments of one engine on a target.

A target used to hold exactly one deployment per engine, identified by the
engine's service id (``vllm``). That id stays the engine's *default instance*
-- its records, keys, saved servers and URLs keep working unchanged. Further
deployments are *named instances*, ``<engine>@<name>`` (``vllm@small``): each
is its own service record (``<target>:vllm@small``), with its own container or
service directory (``clio-vllm-small``), its own loopback port (chosen free on
the target when the person names none), its own per-launch key
(``managed-server:<target>:vllm@small``) and its own ownership ledger.

The model router (:mod:`~clio_agent.gact.infrastructure.model_router`) is one
more managed service on the target, :data:`ROUTER_SERVICE`, that serves every
running instance's model behind one address and one key.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable

from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec, ServiceRecord

#: Separates an engine from an instance name in a service id.
INSTANCE_SEPARATOR = "@"
#: Engines that may run as several named instances on one target.
INSTANCE_ENGINES = frozenset({"vllm"})
#: The managed router service (one per target), upstream LiteLLM Proxy.
ROUTER_SERVICE = "model_router"
#: The configuration key of a service's loopback port.
PORT_FIELD = "port"
#: Printed by :func:`free_port_command` before the port the target chose.
FREE_PORT_MARKER = "CLIO_FREE_PORT"

_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")

Execute = Callable[[CommandSpec], Awaitable[CommandResult]]


class InstanceNameError(ValueError):
    """A service id names an instance CLIO cannot create (``reason`` is the typed code)."""

    reason = "invalid_instance_name"


def split_service_id(service_id: str) -> tuple[str, str]:
    """``(engine, name)`` of a service id; ``name`` is ``""`` for an engine's default instance."""

    engine, _, name = service_id.partition(INSTANCE_SEPARATOR)
    return engine, name


def engine_of(service_id: str) -> str:
    """The engine (or plain service) a service id belongs to: ``vllm@small`` -> ``vllm``."""

    return split_service_id(service_id)[0]


def instance_name(service_id: str) -> str:
    """The instance name of ``service_id`` (``""`` for a default instance)."""

    return split_service_id(service_id)[1]


def is_named_instance(service_id: str) -> bool:
    """Whether ``service_id`` names a non-default instance of an engine."""

    return bool(instance_name(service_id))


def validate_service_id(service_id: str) -> None:
    """Refuse an instance id CLIO cannot deploy (an unknown engine or a malformed name).

    Raises:
        InstanceNameError: For ``<engine>@<name>`` with an engine that has no
            instances or a name that is not 1-32 lowercase letters, digits or
            inner hyphens (the name becomes part of container and directory names).
    """

    engine, name = split_service_id(service_id)
    if INSTANCE_SEPARATOR not in service_id:
        return
    if engine not in INSTANCE_ENGINES:
        raise InstanceNameError(f"{engine or service_id!r} cannot run as named instances")
    if not _NAME.fullmatch(name):
        raise InstanceNameError(
            f"An instance name is 1-32 lowercase letters, digits or inner hyphens (got {name!r})"
        )


def instance_service_id(engine: str, name: str = "") -> str:
    """The service id of ``engine``'s instance ``name`` (the engine itself when unnamed)."""

    service_id = f"{engine}{INSTANCE_SEPARATOR}{name}" if name else engine
    validate_service_id(service_id)
    return service_id


def instance_container_name(base: str, service_id: str) -> str:
    """The container and service-directory name of an instance (``clio-vllm-small``)."""

    name = instance_name(service_id)
    return f"{base}-{name}" if name else base


def instance_label(label: str, service_id: str) -> str:
    """The person-facing label of an instance: ``vLLM`` -> ``vLLM (small)``."""

    name = instance_name(service_id)
    return f"{label} ({name})" if name else label


def instances_on(
    records: Iterable[ServiceRecord], target_id: str, engine: str
) -> list[ServiceRecord]:
    """Every deployment of ``engine`` on ``target_id`` (default instance first, then by name)."""

    return sorted(
        (
            row
            for row in records
            if row.target_id == target_id and engine_of(row.service_id) == engine
        ),
        key=lambda row: (is_named_instance(row.service_id), row.service_id),
    )


def named_instance_ids(records: Iterable[ServiceRecord], target_id: str) -> list[str]:
    """The named instances recorded on ``target_id`` (their catalog rows)."""

    return sorted(
        row.service_id
        for row in records
        if row.target_id == target_id and is_named_instance(row.service_id)
    )


def needs_port(service_id: str, action: str, configuration: dict[str, str]) -> bool:
    """Whether CLIO must choose a free loopback port for this launch.

    Named instances and the router have no fixed port (two deployments of one
    engine cannot share one); a default instance keeps its engine's port.
    """

    return (
        (is_named_instance(service_id) or service_id == ROUTER_SERVICE)
        and action in {"install", "reinstall", "start"}
        and not configuration.get(PORT_FIELD, "").strip()
    )


_FREE_PORT_SCRIPT = (
    "import socket, sys\n"
    "taken = {int(port) for port in sys.argv[1:]}\n"
    "for _ in range(64):\n"
    "    with socket.socket() as probe:\n"
    "        probe.bind(('127.0.0.1', 0))\n"
    "        port = probe.getsockname()[1]\n"
    "    if port >= 1024 and port not in taken:\n"
    f"        print('{FREE_PORT_MARKER}', port)\n"
    "        sys.exit(0)\n"
    "sys.exit(3)\n"
)


def free_port_command(taken: Iterable[int]) -> CommandSpec:
    """Ask the target's own OS for a free loopback port not ``taken`` by another record."""

    return CommandSpec(
        program="python3",
        args=["-c", _FREE_PORT_SCRIPT, *(str(port) for port in sorted(set(taken)))],
        timeout_seconds=30,
    )


async def allocate_port(execute: Execute, taken: Iterable[int]) -> int:
    """A loopback port free on the target right now and not recorded for another deployment.

    Raises:
        ValueError: ``port_unavailable`` when the target could not name one.
    """

    result = await execute(free_port_command(taken))
    for line in result.stdout.splitlines():
        marker, _, value = line.strip().partition(" ")
        if marker == FREE_PORT_MARKER and value.isdigit() and result.exit_code == 0:
            return int(value)
    detail = (result.stderr or result.stdout).strip()
    raise ValueError(
        "port_unavailable: CLIO could not find a free loopback port on this host"
        + (f" ({detail})" if detail else "")
        + "; set a port for this deployment."
    )


__all__ = [
    "FREE_PORT_MARKER",
    "INSTANCE_ENGINES",
    "INSTANCE_SEPARATOR",
    "PORT_FIELD",
    "ROUTER_SERVICE",
    "InstanceNameError",
    "allocate_port",
    "engine_of",
    "free_port_command",
    "instance_container_name",
    "instance_label",
    "instance_name",
    "instance_service_id",
    "instances_on",
    "is_named_instance",
    "named_instance_ids",
    "needs_port",
    "split_service_id",
    "validate_service_id",
]
