"""Declared raw TCP endpoints a Linux-fenced child reaches through CLIO's chokepoint.

Behind the Linux fence a child's only route out is the proxy. Clients that read the proxy
variables, or use ssh (which :mod:`fence_ssh` routes), get through. A raw TCP client does not:
Slurm's ``sbatch`` and ``squeue`` dial ``slurmctld`` directly and fail with "Unable to contact
slurm controller". A site lists such endpoints in ``sandbox.forward_tcp`` (or
``CLIO_SANDBOX_FORWARD_TCP``, comma-separated ``host:port``), and every fenced child starts
behind the helper's forwarder (``net_connect.py forward``):

* When the host resolves to a loopback address on this machine (a login node that is also the
  controller), the forwarder listens on that same address and port inside the sandbox, so the
  client needs no change.
* Otherwise the host gets its own ``127.0.0.N`` address (N from 2) and the forwarder listens
  there on each declared port. A host given by name is also renamed for the child: the
  forwarder gives the program a private ``/etc/hosts`` (in a user and mount namespace of its
  own) in which the name points at that address, so ``sbatch`` resolving ``ares`` reaches the
  listener unchanged. The child's :data:`PUBLISHED_ENV` lists every such endpoint
  (``host:port=127.0.0.N:port``), for a tool that is given an address, or when the kernel
  does not allow the namespace.

Each forwarded connection travels inside the relay tunnel to CLIO's proxy, which records it
and asks the person about a host its policy does not cover, like any private host. Nothing is
forwarded without a declaration, and other platforms' fences are unchanged.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

#: Comma-separated ``host:port`` endpoints to forward (config key ``sandbox.forward_tcp``).
FORWARD_ENV = "CLIO_SANDBOX_FORWARD_TCP"
#: Where a non-loopback endpoint listens inside the sandbox: ``host:port=address:port``, comma
#: separated.
PUBLISHED_ENV = "CLIO_FORWARDED_TCP"


def configured_endpoints() -> list[tuple[str, int]]:
    """The declared ``(host, port)`` endpoints; a malformed entry is logged and skipped."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load
    from clio_agent.runtime.net_connect import _split_authority  # noqa: PLC0415

    try:
        raw = conf.resolve(
            "sandbox.forward_tcp", env="CLIO_SANDBOX_FORWARD_TCP", default=[], cast=conf.as_csv
        )
    except (TypeError, ValueError) as exc:
        logger.warning("sandbox.forward_tcp unreadable reason=forward_tcp_invalid error=%r", exc)
        return []
    endpoints: list[tuple[str, int]] = []
    for entry in raw or []:
        try:
            endpoint = _split_authority(str(entry))
        except ValueError as exc:
            logger.warning("sandbox.forward_tcp entry skipped reason=forward_tcp_invalid %s", exc)
            continue
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    return endpoints


def _loopback_addresses(host: str, port: int) -> list[str]:
    """The loopback addresses ``host`` resolves to on this machine, in resolver order."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    found: list[str] = []
    for _family, _type, _proto, _name, sockaddr in infos:
        address = str(sockaddr[0]).split("%", 1)[0]
        try:
            loopback = ipaddress.ip_address(address).is_loopback
        except ValueError:
            continue
        if loopback and address not in found:
            found.append(address)
    return found


def _authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def forward_plan(
    endpoints: Sequence[tuple[str, int]],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """The forwarder's ``bind:port=host:port`` pairs, its name remap, and the published env."""
    pairs: list[str] = []
    remap: dict[str, str] = {}
    published: list[str] = []
    assigned: dict[str, str] = {}
    taken: set[tuple[str, int]] = set()
    for host, port in endpoints:
        loopback = _loopback_addresses(host, port)
        if not loopback and host not in assigned:
            assigned[host] = f"127.0.0.{len(assigned) + 2}"
            if not _is_address(host):
                remap[host] = assigned[host]
        for address in loopback or [assigned[host]]:
            if (address, port) in taken:
                continue
            taken.add((address, port))
            pairs.append(f"{_authority(address, port)}={_authority(host, port)}")
            if not loopback:
                published.append(f"{_authority(host, port)}={_authority(address, port)}")
    env = {PUBLISHED_ENV: ",".join(published)} if published else {}
    return pairs, remap, env


def wrap_forwarding(
    command: str, args: Sequence[str], net_env: Mapping[str, str]
) -> tuple[str, list[str], dict[str, str]]:
    """Start ``command`` behind the forwarder when the Linux fence routes it through the relay.

    ``net_env`` is the child's egress overlay; only the Linux proxy fence sets the relay
    variable there. Returns the argv to fence and the env to add (both unchanged without a
    declared endpoint).
    """
    from clio_agent.runtime import net_connect  # noqa: PLC0415

    if not net_env.get(net_connect.RELAY_VARIABLE):
        return command, list(args), {}
    endpoints = configured_endpoints()
    if not endpoints:
        return command, list(args), {}
    pairs, remap, env = forward_plan(endpoints)
    helper = str(Path(net_connect.__file__).resolve())
    hosts = [f"--hosts={','.join(f'{n}={a}' for n, a in remap.items())}"] if remap else []
    argv = ["-I", helper, "-", "forward", *hosts, *pairs, "--", command, *args]
    return sys.executable, argv, env


__all__ = [
    "FORWARD_ENV",
    "PUBLISHED_ENV",
    "configured_endpoints",
    "forward_plan",
    "wrap_forwarding",
]
