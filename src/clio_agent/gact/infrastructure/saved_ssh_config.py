"""The person's saved SSH hosts as an OpenSSH config file other programs can read.

CLIO keeps its SSH hosts in ``infrastructure.json``. A plugin's server that offers
"hosts you already have" reads OpenSSH config, not CLIO's store, so the store also
writes this file: one ``Host <alias>`` block per saved SSH host, with ``HostName``,
``User`` and ``Port`` from its route. ``IdentityFile`` is written only when the route
names a key file and ``ProxyJump`` only when it names jump hosts. No secret is ever
written: a route holds none. The alias is a slug of the host's label (its id when
the label has no usable characters), unique within the file.

A plugin's ``mcp.json`` reaches the file as ``${CLIO_SAVED_SSH_CONFIG}``
(:data:`clio_agent.plugins.servers.SAVED_SSH_CONFIG_VAR`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from clio_agent.gact.infrastructure.models import InfrastructureTarget
from clio_agent.platform_paths import atomic_write_text

FILE_NAME = "saved-ssh-config"
_HEADER = (
    "# Written by CLIO: the SSH hosts saved in this CLIO.\n"
    "# CLIO rewrites this file whenever its hosts change; edit the hosts in CLIO.\n"
)
_UNSAFE = re.compile(r"[^a-z0-9._-]+")


def _slug(text: str) -> str:
    return _UNSAFE.sub("-", text.strip().casefold()).strip("-.")[:63]


def _value(text: str) -> str | None:
    """``text`` as one OpenSSH config argument, or ``None`` when it cannot be one."""

    if not text or '"' in text:
        return None
    return f'"{text}"' if any(ch.isspace() or ch == "#" for ch in text) else text


def render(targets: Iterable[InfrastructureTarget]) -> str:
    """The config text for ``targets``: their SSH hosts, in the order given."""

    blocks: list[str] = []
    taken: set[str] = set()
    for target in targets:
        route = target.ssh
        if target.kind != "ssh" or route is None:
            continue
        hostname = _value(route.host or route.profile)
        if hostname is None:
            continue
        base = _slug(target.label) or _slug(target.id) or "host"
        alias, suffix = base, 2
        while alias in taken:
            alias, suffix = f"{base}-{suffix}", suffix + 1
        taken.add(alias)
        lines = [f"Host {alias}", f"    HostName {hostname}"]
        user = _value(route.user)
        if user:
            lines.append(f"    User {user}")
        lines.append(f"    Port {route.port}")
        identity = _value(route.identity_file)
        if identity:
            lines.append(f"    IdentityFile {identity}")
        jumps = [hop for hop in (_value(item) for item in route.jump_hosts) if hop]
        if jumps and len(jumps) == len(route.jump_hosts):
            lines.append(f"    ProxyJump {','.join(jumps)}")
        blocks.append("\n".join(lines) + "\n")
    return _HEADER + "".join(f"\n{block}" for block in blocks)


def write(path: Path, targets: Iterable[InfrastructureTarget]) -> str:
    """Write ``targets``' config to ``path`` atomically; return the text written."""

    text = render(targets)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
    return text
