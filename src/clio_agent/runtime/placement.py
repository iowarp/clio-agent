"""Placement — decide which clio-core mailbox (role queue) a delegated job goes to.

The orchestrator never hard-codes a node. It asks ``placement.mailbox_for(role, hints)``
and submits there; *which* workers drain that queue, and on which node they run, is a
deployment decision. The default is a **role-queue pull** model — one queue per role,
workers work-steal — which decouples the caller from physical placement entirely.

Behind this seam a real scheduler (data-affinity, least-loaded, GPU-availability) can be
dropped in later via the factory without touching callers. Two impls ship: the simple
``role`` default and a ``node``-affinity one that shows the seam extending to node-scoped
queues ("run this where the data already is").
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Optional


class Placement(ABC):
    """Maps a (role, hints) job to the mailbox/queue name it should be submitted to."""

    @abstractmethod
    def mailbox_for(self, role: str, *, hints: Optional[Mapping[str, object]] = None) -> str:
        """Return the clio-core mailbox name a job for ``role`` goes to."""


class RoleQueuePlacement(Placement):
    """Default: one queue per role; idle role-workers pull (work-stealing). Hints are
    ignored — the caller is fully decoupled from where a role's workers physically run."""

    def __init__(self, prefix: str = "clio_core_") -> None:
        self._prefix = prefix

    def mailbox_for(self, role: str, *, hints: Optional[Mapping[str, object]] = None) -> str:
        if not role:
            raise ValueError("role must be non-empty")
        return f"{self._prefix}{role}"


class NodeAffinityPlacement(Placement):
    """A first step toward a real scheduler: when a hint names a node, route to that
    node's role queue ("run where the data is"); otherwise fall back to the plain role
    queue. Demonstrates the seam extending without changing any caller."""

    def __init__(self, prefix: str = "clio_core_", hint_key: str = "node") -> None:
        self._prefix = prefix
        self._hint_key = hint_key

    def mailbox_for(self, role: str, *, hints: Optional[Mapping[str, object]] = None) -> str:
        if not role:
            raise ValueError("role must be non-empty")
        node = str((hints or {}).get(self._hint_key, "") or "")
        return f"{self._prefix}{node}_{role}" if node else f"{self._prefix}{role}"


def make_placement(kind: Optional[str] = None, *, prefix: str = "clio_core_") -> Placement:
    """Build a :class:`Placement` (factory). ``kind`` defaults to ``CLIO_PLACEMENT`` env
    or ``role``. Extend by adding a branch + impl here; callers are unaffected."""
    kind = (kind or os.environ.get("CLIO_PLACEMENT", "role")).strip().lower()
    if kind == "role":
        return RoleQueuePlacement(prefix=prefix)
    if kind in ("affinity", "node"):
        return NodeAffinityPlacement(prefix=prefix)
    raise ValueError(f"unknown placement {kind!r}; expected 'role' or 'affinity'")
