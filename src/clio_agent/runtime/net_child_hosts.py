"""Per-child egress host allowlists for the network chokepoint (LaTeX ledger L6).

A child registered here may reach only these hosts, whatever the workspace gate says: a LaTeX
build reaches its pinned bundle and nothing else. Keyed by the child's channel id; cleared
when the child is released. :mod:`clio_agent.runtime.net_chokepoint` consults it first.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

_CHILD_HOSTS: dict[str, frozenset[str]] = {}
_CHILD_HOSTS_LOCK = threading.Lock()


def restrict_child_hosts(child_id: str, hosts: Iterable[str]) -> None:
    """Limit ``child_id``'s egress to ``hosts`` (exact, case-insensitive host names)."""
    with _CHILD_HOSTS_LOCK:
        _CHILD_HOSTS[child_id] = frozenset(host.lower() for host in hosts)


def release_child_hosts(child_id: str) -> None:
    """Drop ``child_id``'s host allowlist (idempotent)."""
    with _CHILD_HOSTS_LOCK:
        _CHILD_HOSTS.pop(child_id, None)


def _child_host_allowed(child_id: str, host: str) -> bool:
    with _CHILD_HOSTS_LOCK:
        allowed = _CHILD_HOSTS.get(child_id) if child_id else None
    return allowed is None or host.lower() in allowed
