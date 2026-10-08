"""Retain task-capable submission RPCs across cancellation of their turn waiter."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

from clio_agent.gact import context
from clio_agent.tools.mcp_call_progress import ProgressHandler, call_tool_with_progress

logger = logging.getLogger(__name__)


async def submit_tool_call(
    executor: Any,
    client: Any,
    name: str,
    backend_name: str,
    args: dict[str, Any],
    *,
    timeout: float | None,
    progress_handler: ProgressHandler | None,
    namespace: str | None,
) -> Any:
    """Compose existing RPC health/progress with custody for negotiated task backends."""
    from fastmcp.utilities.tasks import TASKS_EXTENSION_ID

    tool = executor._mcp_tools.get(name)
    support = getattr(getattr(tool, "execution", None), "task_support", None)
    extensions = getattr(getattr(client, "server_capabilities", None), "extensions", {}) or {}
    capable = support != "forbidden" and (
        support in {"required", "optional"}
        or bool(namespace and executor._namespace_direct_routes.get(namespace))
        or TASKS_EXTENSION_ID in extensions
    )
    return await submit_with_custody(
        executor,
        call_tool_with_progress(
            client, backend_name, args, timeout=timeout, progress_handler=progress_handler
        ),
        capable=capable,
    )


async def submit_with_custody(executor: Any, operation: Awaitable[Any], *, capable: bool) -> Any:
    """Let Stop release the waiter while an already sent task request records its outcome.

    This retains the existing RPC and its transport, never repeats tools/call.
    Submission health limits still apply inside the operation. No public handle
    exists until the backend accepts and the existing task extension persists it.
    """
    app = context.active_app()
    if not capable or app is None or app.state.mcp_app_loop is not asyncio.get_running_loop():
        return await operation
    from clio_agent.gact.task_supervisor import task_supervisor

    supervisor = task_supervisor(app)
    supervisor.retained_executors.add(executor)
    pending = asyncio.ensure_future(operation)
    executor._pending_task_submissions.add(pending)
    pending.add_done_callback(lambda task: _submitted(executor, supervisor, task))
    return await asyncio.shield(pending)


def _submitted(executor: Any, supervisor: Any, task: asyncio.Future[Any]) -> None:
    executor._pending_task_submissions.discard(task)
    if not task.cancelled() and task.exception() is not None:
        error = task.exception()
        assert error is not None
        logger.error(
            "Task submission failed; no acceptance was acknowledged",
            exc_info=(type(error), error, error.__traceback__),
        )
    if not executor._pending_task_submissions and not executor._accepted_task_handles:
        supervisor.retained_executors.discard(executor)
        if executor._close_requested:
            asyncio.create_task(executor.aclose())
