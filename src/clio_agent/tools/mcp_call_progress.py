"""Compose MCP progress forwarding with activity-driven timeout tracking."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

ProgressHandler = Callable[[float, float | None, str | None], Awaitable[None]]


async def call_tool_with_progress(
    client: Any,
    name: str,
    args: Mapping[str, Any],
    *,
    timeout: float | None,
    progress_handler: ProgressHandler | None,
) -> Any:
    """Call one MCP tool while composing progress with its activity backstop."""

    from clio_agent.tools.mcp_header_mismatch import (  # noqa: PLC0415
        call_tool_with_header_retry,
    )
    from clio_agent.tools.mcp_wait_ladder import (  # noqa: PLC0415
        ActivityClock,
        activity_progress_handler,
        run_with_activity_backstop,
    )

    if timeout is None:
        return await call_tool_with_header_retry(
            client,
            name,
            dict(args),
            progress_handler=progress_handler,
        )

    activity = ActivityClock()
    touch_activity = activity_progress_handler(activity)

    async def forward_activity(
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        await touch_activity(progress, total, message)
        if progress_handler is not None:
            await progress_handler(progress, total, message)

    return await run_with_activity_backstop(
        call_tool_with_header_retry(
            client,
            name,
            dict(args),
            progress_handler=forward_activity,
        ),
        tool=name,
        timeout=timeout,
        activity=activity,
    )


__all__ = ["ProgressHandler", "call_tool_with_progress"]
