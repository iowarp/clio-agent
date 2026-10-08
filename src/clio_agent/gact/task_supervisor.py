"""Application lifetime supervision over existing task owners and transports."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from dataclasses import replace
from typing import Any

from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.task_projection import TERMINAL
from clio_agent.tools.mcp_task_records import TaskKey

logger = logging.getLogger(__name__)


class TaskSupervisor:
    """Own drivers independently of turns; persisted workload rows remain authoritative."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.delivery_lock = threading.RLock()
        self.drivers: dict[str, asyncio.Task[Any]] = {}
        self.cancellers: dict[str, Callable[[], Coroutine[Any, Any, Any]]] = {}
        self.cancellation_requests: dict[str, Future[Any]] = {}
        self.retained_executors: set[Any] = set()
        self.closing = False

    def supervise(
        self,
        handle: str,
        operation: Coroutine[Any, Any, Any],
        cancel: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    ) -> None:
        """Retain a submitted operation on the application loop without joining its result."""
        if self.closing:
            raise RuntimeError("Application task supervisor is shutting down")
        if handle in self.drivers:
            raise RuntimeError("Task already has a driver")
        self.drivers[handle] = asyncio.create_task(operation, name=f"clio-task:{handle}")
        if cancel is not None:
            self.cancellers[handle] = cancel
        self.drivers[handle].add_done_callback(lambda task: self._finished(handle, task))

    def _finished(self, handle: str, task: asyncio.Task[Any]) -> None:
        self.drivers.pop(handle, None)
        self.cancellers.pop(handle, None)
        if not task.cancelled() and task.exception() is not None:
            error = task.exception()
            assert error is not None
            logger.error(
                "task driver failed handle=%s",
                handle,
                exc_info=(type(error), error, error.__traceback__),
            )

    def request_cancel(self, row: dict[str, Any]) -> bool:
        """Request cancellation on the owning loop, with idempotent persisted intent."""
        if row["task_kind"] == "Subagent":
            from clio_agent.gact.loop_handoff import call_on_loop
            from clio_agent.gact.turn_spawn import cancel_agent_task

            return call_on_loop(
                self.app.state.mcp_app_loop,
                lambda: cancel_agent_task(self.app, row["id"]),
                op="cancel task subtree",
            )
        if row["effective_status"] in TERMINAL:
            return False
        store = app_task_store(self.app)
        key = TaskKey.from_wire(row["key"])
        with self.delivery_lock:
            record = store.get(key)
            if record is None:
                raise ValueError("Task owner is unavailable")
            if record.cancel_acknowledged:
                return True
            callback = self.cancellers.get(row["handle"])
            if callback is None:
                raise RuntimeError("Task transport is disconnected; cancellation is unavailable")
            store.put(replace(record, cancel_requested=True))
            future = self.cancellation_requests.get(row["handle"])
            if future is None:
                future = asyncio.run_coroutine_threadsafe(callback(), self.app.state.mcp_app_loop)
                self.cancellation_requests[row["handle"]] = future
        try:
            future.result(timeout=30)
        except Exception as exc:
            if future.done():
                with self.delivery_lock:
                    self.cancellation_requests.pop(row["handle"], None)
            raise RuntimeError(f"Cancellation request was not acknowledged: {exc}") from exc
        with self.delivery_lock:
            current = store.get(key)
            if current is not None:
                store.put(replace(current, cancel_acknowledged=True))
        return True

    async def shutdown(self) -> None:
        """Settle owned drivers before closing their retained MCP transports."""
        self.closing = True
        # Submission is a single already-sent RPC, independent of a stopped
        # turn. Service shutdown may interrupt that RPC but must never replay it.
        submissions = [
            task
            for executor in self.retained_executors
            for task in executor._pending_task_submissions
        ]
        for task in submissions:
            task.cancel()
        if submissions:
            await asyncio.gather(*submissions, return_exceptions=True)
        tasks = list(self.drivers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for executor in list(self.retained_executors):
            await executor.aclose(force=True)
        self.retained_executors.clear()


def task_supervisor(app: Any) -> TaskSupervisor:
    """Return the application's supervisor (also lazy for isolated test applications)."""
    existing = getattr(app.state, "task_supervisor", None)
    if existing is None:
        existing = TaskSupervisor(app)
        app.state.task_supervisor = existing
    return existing
