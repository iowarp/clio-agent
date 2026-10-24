"""The app's memory service and usage counters, built once per process.

Memory lives on clio-core, so the service exists only once the process ARC is
attached (:func:`clio_agent.gact.server_boot.process_arc`). In History mode the
platform cannot run clio-core and memory is unavailable with a typed reason; nothing
falls back to another store.
"""

from __future__ import annotations

import threading
from typing import Any

from clio_agent.errors import ClioError
from clio_agent.gact.memory.service import MemoryService
from clio_agent.gact.memory.usage import UsageCounters

_LOCK = threading.Lock()


class MemoryUnavailable(ClioError):
    """Long-term memory cannot run in this process (typed reason)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"long-term memory is unavailable (reason={reason})",
            error_type="memory_unavailable",
            details={"reason": reason},
        )


def memory_service(app: Any) -> MemoryService:
    """The process's memory service; call off the event loop (it may attach clio-core).

    Raises:
        MemoryUnavailable: The process runs in History mode (no clio-core).
    """
    existing = getattr(app.state, "memory_service", None)
    if existing is not None:
        return existing
    with _LOCK:
        existing = getattr(app.state, "memory_service", None)
        if existing is not None:
            return existing
        from clio_agent.gact.memory.store import open_memory_store  # noqa: PLC0415
        from clio_agent.gact.server_boot import process_arc  # noqa: PLC0415 - import cycle

        if process_arc(app) is None:
            raise MemoryUnavailable("history_mode")
        service = MemoryService(open_memory_store())
        app.state.memory_usage = UsageCounters(service.store)
        app.state.memory_service = service
        return service


def memory_usage(app: Any) -> UsageCounters:
    """The process's usage counters (built with the service)."""
    memory_service(app)
    return app.state.memory_usage


__all__ = ["MemoryUnavailable", "memory_service", "memory_usage"]
