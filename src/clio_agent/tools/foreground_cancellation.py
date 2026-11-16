"""Foreground coroutine cancellation bridged onto MCP's async request lifecycle."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable
from typing import Any

from clio_agent.errors import MCP_WIRE_CANCELLATION_UNAVAILABLE, CancellationError

_CANCELLATION_POLL_SECONDS = 0.05
_MCP_WIRE_CANCELLATION_SETTLE_SECONDS = 1.0


#: Why a call was cancelled, as the cancellation checker reports it (``cause``).
SERVER_SHUTDOWN = "server_shutdown"
_SHUTDOWN_TEXT = "tool call stopped because the agent service is shutting down"


def cancellation_cause() -> str:
    """``server_shutdown`` when the agent service stopping cancels this call, else ``""``.

    The live turn's cancellation checker carries the cause (``checker.cause``, set by
    gact); a call the person stopped has none. Without it the model was told
    "cancelled by client" for a call the shutdown stopped, and said so (CC-O2).
    """
    from clio_agent.tools.execution import current_tool_runtime  # noqa: PLC0415 - cycle

    cause = getattr(current_tool_runtime().cancellation_checker, "cause", None)
    return str(cause() or "") if callable(cause) else ""


def _tool_cancellation_error(
    tool: str,
    stage: str,
    *,
    wire_settled: bool | None = None,
    before_approval: str = "",
) -> CancellationError:
    """The error a cancelled call raises, naming who cancelled it.

    ``before_approval`` names what did not run when the call was cancelled while its
    approval waited.
    """
    execution_cancellation = "cooperative"
    wire_cancellation = "not_needed"
    details: dict[str, Any] = {"tool": tool, "stage": stage}
    cause = cancellation_cause()
    if cause == SERVER_SHUTDOWN:
        details["cause"] = SERVER_SHUTDOWN
        message = (
            f"tool call stopped before it was approved because the agent service is "
            f"shutting down: {before_approval}"
            if before_approval
            else _SHUTDOWN_TEXT
        )
    else:
        message = (
            f"tool call cancelled by client before it was approved: {before_approval}"
            if before_approval
            else "tool call cancelled by client"
        )
    if wire_settled is not None:
        wire_cancellation = "requested" if wire_settled else "unavailable"
        if wire_settled:
            execution_cancellation = "mcp_wire"
        else:
            details["reason"] = MCP_WIRE_CANCELLATION_UNAVAILABLE
    details.update(
        {
            "execution_cancellation": execution_cancellation,
            "mcp_wire_cancellation": wire_cancellation,
        }
    )
    return CancellationError(message, details=details)


def _run_foreground_coroutine(
    loop: asyncio.AbstractEventLoop,
    coro: Any,
    *,
    timeout: float | None,
    action: str,
    cancellation_checker: Callable[[], bool] | None = None,
    cancellation_error: Callable[[bool], CancellationError] | None = None,
    wait_for_settlement: bool = False,
) -> Any:
    """Run ``coro`` on ``loop`` from this (foreground) thread, cooperatively
    cancellable while it runs.

    ``timeout=None`` means NO deadline (#1225 D1-REVISED): a
    ``wait_for_terminal`` commitment must never be truncated by an arbitrary
    internal TTL -- the only bounds left are the door's own task semantics and
    an explicit caller-supplied budget. An unbounded call still polls at
    ``_CANCELLATION_POLL_SECONDS`` when a ``cancellation_checker`` is
    supplied, so it remains cooperatively cancellable; it is simply never
    abandoned on its own.
    """
    settled = threading.Event()
    gate = threading.Lock()
    started = False

    async def tracked() -> Any:
        nonlocal started
        with gate:
            if settled.is_set():
                raise asyncio.CancelledError
            started = True
        try:
            return await coro
        finally:
            settled.set()

    future = asyncio.run_coroutine_threadsafe(tracked(), loop)

    def cancel() -> bool:
        with gate:
            if not started:
                coro.close()
                settled.set()
            return future.cancel()

    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        wait_seconds: float | None
        if cancellation_checker is not None:
            wait_seconds = (
                _CANCELLATION_POLL_SECONDS
                if remaining is None
                else min(remaining, _CANCELLATION_POLL_SECONDS)
            )
        else:
            wait_seconds = remaining
        try:
            return future.result(timeout=wait_seconds)
        except concurrent.futures.TimeoutError as exc:
            if future.done():
                return future.result()
            if cancellation_checker is not None:
                try:
                    cancel_requested = cancellation_checker()
                except Exception:
                    cancel()
                    settled.wait(
                        None if wait_for_settlement else _MCP_WIRE_CANCELLATION_SETTLE_SECONDS
                    )
                    raise
                if cancel_requested and cancel():
                    # Do not synthesize a request id here. Cancelling this exact call
                    # task makes MCP's dispatcher use the id in its pending table.
                    wire_settled = settled.wait(
                        None if wait_for_settlement else _MCP_WIRE_CANCELLATION_SETTLE_SECONDS
                    )
                    if cancellation_error is not None:
                        raise cancellation_error(wire_settled) from None
                    raise CancellationError(
                        "operation cancelled by client",
                        details={"execution_cancellation": "mcp_wire"},
                    ) from None
            if deadline is None or time.monotonic() < deadline:
                continue
            cancel()
            raise TimeoutError(f"{action} timed out after {timeout:g}s") from exc


def inline_timeout(executor: Any, tool: str, arguments: dict[str, Any]) -> float | None:
    """Keep finite CLI call budgets; product task owners govern post-acceptance lifetime."""
    from clio_agent.tools.task_call_context import application_loop

    if application_loop() is not None:
        return None
    return executor._timeout_budget_for_call(tool, arguments).seconds
