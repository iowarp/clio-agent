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


def _tool_cancellation_error(
    tool: str,
    stage: str,
    *,
    wire_settled: bool | None = None,
) -> CancellationError:
    execution_cancellation = "cooperative"
    executor_work_may_continue = False
    wire_cancellation = "not_needed"
    details: dict[str, Any] = {"tool": tool, "stage": stage}
    if wire_settled is not None:
        wire_cancellation = "requested" if wire_settled else "unavailable"
        executor_work_may_continue = not wire_settled
        if wire_settled:
            execution_cancellation = "mcp_wire"
        else:
            details["reason"] = MCP_WIRE_CANCELLATION_UNAVAILABLE
    details.update(
        {
            "execution_cancellation": execution_cancellation,
            "executor_work_may_continue": executor_work_may_continue,
            "mcp_wire_cancellation": wire_cancellation,
        }
    )
    return CancellationError("tool call cancelled by client", details=details)


def _run_foreground_coroutine(
    loop: asyncio.AbstractEventLoop,
    coro: Any,
    *,
    timeout: float,
    action: str,
    cancellation_checker: Callable[[], bool] | None = None,
    cancellation_error: Callable[[bool], CancellationError] | None = None,
) -> Any:
    settled = threading.Event()

    async def tracked() -> Any:
        try:
            return await coro
        finally:
            settled.set()

    future = asyncio.run_coroutine_threadsafe(tracked(), loop)
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        wait_seconds = (
            min(remaining, _CANCELLATION_POLL_SECONDS)
            if cancellation_checker is not None
            else remaining
        )
        try:
            return future.result(timeout=wait_seconds)
        except concurrent.futures.TimeoutError as exc:
            if future.done():
                return future.result()
            if cancellation_checker is not None:
                try:
                    cancel_requested = cancellation_checker()
                except Exception:
                    future.cancel()
                    settled.wait(_MCP_WIRE_CANCELLATION_SETTLE_SECONDS)
                    raise
                if cancel_requested and future.cancel():
                    # Do not synthesize a request id here. Cancelling this exact call
                    # task makes MCP's dispatcher use the id in its pending table.
                    wire_settled = settled.wait(_MCP_WIRE_CANCELLATION_SETTLE_SECONDS)
                    if cancellation_error is not None:
                        raise cancellation_error(wire_settled) from None
                    raise CancellationError(
                        "operation cancelled by client",
                        details={
                            "execution_cancellation": "mcp_wire",
                            "executor_work_may_continue": not wire_settled,
                        },
                    ) from None
            if time.monotonic() < deadline:
                continue
            future.cancel()
            raise TimeoutError(f"{action} timed out after {timeout:g}s") from exc
