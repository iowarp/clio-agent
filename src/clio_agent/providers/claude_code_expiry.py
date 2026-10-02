"""How long a Claude Code call may wait, and the typed errors when it stops progressing.

Two waits, each on its own progress signal (#1577 3.6); never one flat bound over the
connect plus the whole streamed reply, which killed a long answer that was still
streaming and treated a slow machine as a failure:

* **Connect** (:func:`connect_while_working`): the CLI subprocess starting and answering
  ``initialize``. After :data:`CONNECT_FIRST_WAIT_S` it is waited for only while the CLI
  process tree keeps working (CPU / I/O), each :data:`CONNECT_STRETCH_S`, up to the
  :data:`~clio_agent.runtime.process_progress.DEFAULT_CEILING_S` ceiling; else
  :class:`ClaudeCodeConnectTimeout`.
* **Stream** (:func:`relay_with_idle_bound`): the gap between two SDK messages may not
  exceed the idle bound (``limits.lm_inter_token_idle_s``); a reply that keeps streaming
  runs as long as it needs. A silent gap raises :class:`ClaudeCodeIdleTimeout`.

Both errors subclass ``TimeoutError``; the engine maps them to ``dspy.lm15``'s
``TimeoutError`` with their own words, so the trace says WHICH wait expired.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from clio_agent.runtime.process_progress import NoProgressError, await_while_working, tree_work

logger = logging.getLogger(__name__)

#: A connect answering within this costs no progress check.
CONNECT_FIRST_WAIT_S = 60.0
#: After the first wait, each stretch of this length must show CLI work.
CONNECT_STRETCH_S = 30.0


class ClaudeCodeConnectTimeout(TimeoutError):
    """The Claude Code CLI did not finish connecting and stopped making progress."""

    def __init__(self, waited_s: float, reason: str) -> None:
        self.waited_s = waited_s
        self.reason = reason
        super().__init__(
            f"Claude Code did not connect: no answer after {waited_s:.0f}s and the CLI "
            f"made no progress (connect {reason})"
        )


class ClaudeCodeIdleTimeout(TimeoutError):
    """The Claude Code stream went silent for longer than the idle bound."""

    def __init__(self, idle_s: float, received: int) -> None:
        self.idle_s = idle_s
        self.received = received
        noun = "message" if received == 1 else "messages"
        super().__init__(
            f"Claude Code stream went idle: no message for {idle_s:g}s "
            f"after {received} {noun} (limits.lm_inter_token_idle_s)"
        )


def cli_work(client: Any) -> float | None:
    """Work so far of the client's CLI process tree; ``None`` before spawn / after exit."""
    process = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(process, "pid", None)
    return tree_work(pid) if isinstance(pid, int) else None


async def connect_while_working(client: Any) -> None:
    """``client.connect()`` waited for while its CLI works; typed failure otherwise.

    On failure the half-connected client is disconnected (logged if that fails too) so
    no CLI subprocess outlives the abandoned connect.
    """
    try:
        await await_while_working(
            client.connect(),
            op="claude_code.connect",
            work=lambda: cli_work(client),
            first_wait_s=CONNECT_FIRST_WAIT_S,
            stretch_s=CONNECT_STRETCH_S,
        )
    except NoProgressError as exc:
        from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415 - import cycle
            transient_transport_error_types,
        )

        cleanup_errors: tuple[type[BaseException], ...] = (
            *transient_transport_error_types(),
            OSError,
            RuntimeError,
        )
        try:
            await client.disconnect()
        except cleanup_errors as disconnect_exc:
            logger.warning(
                "claude code connect abandoned; disconnect failed "
                "reason=connect_cleanup_failed error=%r",
                disconnect_exc,
            )
        raise ClaudeCodeConnectTimeout(exc.waited_s, exc.reason) from exc


async def relay_with_idle_bound(
    client: Any,
    query_input: Any,
    *,
    session_id: str,
    idle_s: float | None,
    put: Callable[[Any], None],
) -> None:
    """Send one query and ``put`` each reply message; the gap between messages is bounded.

    The bound restarts on every message, so only silence expires it. ``idle_s=None``
    leaves the stream unbounded (callers that own their own bound).
    """
    loop = asyncio.get_running_loop()
    received = 0
    idle = asyncio.timeout(idle_s)
    try:
        async with idle:
            await client.query(query_input, session_id=session_id)
            async for message in client.receive_response():
                put(message)
                received += 1
                if idle_s is not None:
                    idle.reschedule(loop.time() + idle_s)
    except TimeoutError as exc:
        if idle_s is not None and idle.expired():
            logger.warning(
                "claude code stream idle reason=stream_idle_timeout idle_s=%g received=%d",
                idle_s,
                received,
            )
            raise ClaudeCodeIdleTimeout(idle_s, received) from exc
        raise


__all__ = [
    "CONNECT_FIRST_WAIT_S",
    "CONNECT_STRETCH_S",
    "ClaudeCodeConnectTimeout",
    "ClaudeCodeIdleTimeout",
    "cli_work",
    "connect_while_working",
    "relay_with_idle_bound",
]
