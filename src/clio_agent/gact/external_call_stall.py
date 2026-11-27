"""Watch one MCP tool call for silence: a visible notice, never a deadline.

Every agent MCP call goes through :meth:`ExternalTasks._call`, a top-level one and a
``tools.*`` call nested in a ``run_code`` cell alike, so this watch covers both. A call
with no reply and no progress notification for ``tools.mcp.no_progress_s`` (30 s) is
reported on its own row ("No reply from relay exec_read for 30s ...") and logged
``mcp_call_stalled``; the next notice waits twice as long (up to 5 minutes), and any
progress from the server starts a fresh window. The call keeps waiting: there is no
ceiling, and stopping the turn cancels it. A connection found dead while the reply is
still pending ends the call at once, typed :data:`EXTERNAL_MCP_CONNECTION_LOST`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from clio_agent.tools.mcp_server_progress import mcp_no_progress_s

logger = logging.getLogger(__name__)

#: The reply of a call whose connection died under it is never coming.
EXTERNAL_MCP_CONNECTION_LOST = "external_mcp_connection_lost"
#: Longest gap between two notices for one silent call (seconds).
MAX_NOTICE_GAP_S = 300.0
#: How a notice says the call is cancelled; a background script's row says its own.
STOP_HINT = "Stop the turn to cancel."
TASK_STOP_HINT = "Stop the task to cancel."

Progress = Callable[[float, float | None, str | None], Awaitable[None]]


def _seconds(value: float) -> str:
    minutes, rest = divmod(int(value), 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


async def await_reply(
    rpc: asyncio.Task[Any],
    *,
    server: str,
    tool: str,
    heard: list[float],
    connected: Callable[[], bool],
    progress_handler: Progress | None,
) -> Any:
    """The reply of ``rpc``; it is never cancelled here (a Stop cancels the caller).

    ``heard[0]`` is the loop time the call was sent or last reported progress; the
    caller's progress callback moves it.
    """

    loop = asyncio.get_running_loop()
    base = mcp_no_progress_s()
    mark, window = heard[0], base
    while True:
        done, _ = await asyncio.wait((rpc,), timeout=max(0.0, mark + window - loop.time()))
        if done:
            return rpc.result()
        if heard[0] > mark:  # the server reported progress: a fresh quiet window
            mark, window = heard[0], base
            continue
        if loop.time() < mark + window:
            continue
        silent = loop.time() - heard[0]
        if not connected():
            logger.warning(
                "%s server=%s tool=%s silent_s=%.0f",
                EXTERNAL_MCP_CONNECTION_LOST,
                server,
                tool,
                silent,
            )
            rpc.cancel()
            raise ConnectionError(
                f"{EXTERNAL_MCP_CONNECTION_LOST}: the connection to {server} closed with "
                f"{tool} still unanswered"
            )
        logger.warning("mcp_call_stalled server=%s tool=%s silent_s=%.0f", server, tool, silent)
        if progress_handler is not None:
            message = (
                f"No reply from {server} {tool} for {_seconds(silent)}; still waiting. {STOP_HINT}"
            )
            try:
                await progress_handler(round(silent, 1), None, message)
            except Exception:  # the notice is best-effort; the call goes on
                logger.debug("reason=mcp_call_stall_undelivered tool=%s", tool, exc_info=True)
        mark, window = loop.time(), min(window * 2, MAX_NOTICE_GAP_S)


__all__ = [
    "EXTERNAL_MCP_CONNECTION_LOST",
    "MAX_NOTICE_GAP_S",
    "STOP_HINT",
    "TASK_STOP_HINT",
    "await_reply",
]
