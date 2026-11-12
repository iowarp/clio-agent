"""How long a Claude Code call may wait, and the typed errors when it stops progressing.

Two waits, each on its own progress signal (never one flat bound over the connect plus
the whole streamed reply):

* **Connect** (:func:`connect_while_working`): the CLI subprocess starting and answering
  ``initialize``. After :data:`CONNECT_FIRST_WAIT_S` it is waited for only while the CLI
  process tree keeps working (CPU / I/O), each :data:`CONNECT_STRETCH_S`, up to the
  :data:`~clio_agent.runtime.progress.DEFAULT_CEILING_S` ceiling; else
  :class:`ClaudeCodeConnectTimeout`.
* **Stream** (:func:`relay_with_idle_bound`): stall detection, never a clock on the
  reply. A reply that keeps streaming runs as long as it needs. When no message arrives
  for the current wait (``limits.lm_stall_wait_s``, 30 s, then doubling per stall up to
  :data:`STALL_MAX_WAIT_S`), the caller's ``on_silence`` decides: keep waiting on this
  stream for the next, longer wait, or end it with :class:`ClaudeCodeIdleTimeout` so
  the engine resends. Either way the person sees the stall and can stop the turn; the
  engine never fails the turn on silence alone.

Both errors subclass ``TimeoutError``; the engine maps them to ``dspy.lm15``'s
``TimeoutError`` with their own words, so the trace says WHICH wait expired.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from clio_agent.runtime.progress import NoProgressError, await_while_working, tree_work

logger = logging.getLogger(__name__)

#: A connect answering within this costs no progress check.
CONNECT_FIRST_WAIT_S = 60.0
#: After the first wait, each stretch of this length must show CLI work.
CONNECT_STRETCH_S = 30.0
#: The first silence that counts as a stall (``limits.lm_stall_wait_s``).
STALL_FIRST_WAIT_S = 30.0
#: Each later wait is this much longer than the one before it.
STALL_GROWTH = 2.0
#: The longest single wait. Retries never stop; this keeps a visible notice coming
#: well inside the turn's no-progress window (``limits.turn_timeout_s``, 900 s).
STALL_MAX_WAIT_S = 300.0


def stall_first_wait_s() -> float:
    """``limits.lm_stall_wait_s`` / ``CLIO_LM_STALL_WAIT_S``: silence before the first retry."""
    from clio_agent import conf  # noqa: PLC0415

    try:
        value = conf.resolve(
            "limits.lm_stall_wait_s",
            env="CLIO_LM_STALL_WAIT_S",
            default=STALL_FIRST_WAIT_S,
            cast=conf.as_float,
        )
    except (ValueError, TypeError):
        return STALL_FIRST_WAIT_S
    return value if value > 0 else STALL_FIRST_WAIT_S


def stall_waits(first_s: float) -> Iterator[float]:
    """The growing waits of one call's stall ladder: ``first``, x2, x2 ... capped, endless."""
    wait = first_s
    cap = max(first_s, STALL_MAX_WAIT_S)
    while True:
        yield wait
        wait = min(wait * STALL_GROWTH, cap)


@dataclass(frozen=True)
class StreamSilence:
    """Relayed in place of a message: the stream has been silent for ``silent_s`` and the
    relay keeps waiting ``next_wait_s`` longer. Never part of the reply."""

    silent_s: float
    next_wait_s: float


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
    """The Claude Code stream sent nothing for one stall wait; the engine resends."""

    def __init__(self, idle_s: float, received: int) -> None:
        self.idle_s = idle_s
        self.received = received
        noun = "message" if received == 1 else "messages"
        super().__init__(
            f"Claude Code stream went idle: no message for {idle_s:g}s "
            f"after {received} {noun} (limits.lm_stall_wait_s)"
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
    on_silence: Callable[[float], float | None] | None = None,
) -> None:
    """Send one query and ``put`` each reply message; silence is a stall, never a deadline.

    Each message restarts the wait at ``idle_s``. When ``idle_s`` passes with no message,
    ``on_silence(silent_s)`` returns how much longer to keep waiting on this stream (a
    :class:`StreamSilence` is ``put`` so the caller can show it), or ``None`` to end the
    stream with :class:`ClaudeCodeIdleTimeout` so the caller can resend. Without
    ``on_silence`` the first silent wait ends it. ``idle_s=None`` waits without bound.

    A ``ping`` stream event is not a message of the reply: the API sends one every 30 s
    on an open stream whether or not the model produces anything, so a stalled
    generation would otherwise hold the wait open forever (seen live: 12 minutes of
    pings and nothing else, the turn running with nothing on screen). Pings are passed
    on but neither restart the wait nor count as received.
    """
    loop = asyncio.get_running_loop()
    received = 0
    keepalives = 0
    try:
        async with asyncio.timeout(idle_s):
            await client.query(query_input, session_id=session_id)
    except TimeoutError as exc:
        _log_stall(idle_s or 0.0, received, keepalives)
        raise ClaudeCodeIdleTimeout(idle_s or 0.0, received) from exc
    replies = aiter(client.receive_response())
    pending: asyncio.Future[Any] | None = None
    silent_since = loop.time()
    deadline = None if idle_s is None else silent_since + idle_s
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(replies))
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            if not done:
                silent_s = loop.time() - silent_since
                more = on_silence(silent_s) if on_silence is not None else None
                if more is None:
                    _log_stall(silent_s, received, keepalives)
                    raise ClaudeCodeIdleTimeout(silent_s, received)
                logger.warning(
                    "claude code stream silent reason=stream_stall_waiting silent_s=%.0f "
                    "next_wait_s=%g received=%d keepalives=%d",
                    silent_s,
                    more,
                    received,
                    keepalives,
                )
                put(StreamSilence(silent_s=silent_s, next_wait_s=more))
                deadline = loop.time() + more
                continue
            arrived, pending = pending, None
            try:
                message = arrived.result()
            except StopAsyncIteration:
                return
            put(message)
            if _is_keepalive(message):
                keepalives += 1
                continue
            received += 1
            silent_since = loop.time()
            if idle_s is not None:
                deadline = silent_since + idle_s
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending


def _log_stall(silent_s: float, received: int, keepalives: int) -> None:
    logger.warning(
        "claude code stream idle reason=stream_idle_timeout idle_s=%g received=%d keepalives=%d",
        silent_s,
        received,
        keepalives,
    )


def _is_keepalive(message: Any) -> bool:
    """A transport keep-alive (the API's ``ping`` stream event), not part of the reply."""
    if type(message).__name__ != "StreamEvent":
        return False
    event = getattr(message, "event", None)
    return isinstance(event, dict) and event.get("type") == "ping"


__all__ = [
    "CONNECT_FIRST_WAIT_S",
    "CONNECT_STRETCH_S",
    "STALL_FIRST_WAIT_S",
    "STALL_GROWTH",
    "STALL_MAX_WAIT_S",
    "ClaudeCodeConnectTimeout",
    "ClaudeCodeIdleTimeout",
    "StreamSilence",
    "cli_work",
    "connect_while_working",
    "relay_with_idle_bound",
    "stall_first_wait_s",
    "stall_waits",
]
