"""The last runtime mount outcome per MCP namespace, for honest SPOTTER availability.

``GET /v1/spotter/availability`` decides statically (declarations resolve, the
provenance handoff projects). A server whose declaration resolves can still
fail to START (c43: the SPOTTER MCP exited on a missing workspace handoff while
availability said ``available=true``). The only place that sees a start
failure is :func:`clio_agent.gact.mcp_readiness.mount_namespaces_for_session`,
which reports it to the one turn that asked and keeps nothing.

This module keeps the most recent typed failure per namespace for the life of
the process, so availability can report it. A later successful mount clears
it. Process-scoped on purpose: a restart re-mounts from scratch, and nothing
here is a durable store.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MountFailure:
    """A namespace's last mount failure: the typed reason and when it happened."""

    namespace: str
    reason: str
    at: float


_LOCK = threading.Lock()
_FAILURES: dict[str, MountFailure] = {}


def record_failure(namespace: str, reason: str) -> None:
    """Remember that ``namespace`` failed to mount with typed ``reason``."""

    with _LOCK:
        _FAILURES[namespace] = MountFailure(namespace=namespace, reason=reason, at=time.time())


def record_success(namespace: str) -> None:
    """Forget any earlier failure: ``namespace`` mounted."""

    with _LOCK:
        _FAILURES.pop(namespace, None)


def last_failure(namespace: str) -> Optional[MountFailure]:
    """The most recent unresolved mount failure of ``namespace``, if any."""

    with _LOCK:
        return _FAILURES.get(namespace)


def reset() -> None:
    """Forget every outcome (tests)."""

    with _LOCK:
        _FAILURES.clear()


__all__ = ["MountFailure", "last_failure", "record_failure", "record_success", "reset"]
