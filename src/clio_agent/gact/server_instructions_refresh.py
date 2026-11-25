"""Re-read one server's instructions after something outside a turn changed them.

A server's instructions may name what it reaches now (its sites, datasets or packages),
and ``search_tools`` and ``offer_plugin`` find a server and its plugin by those words
(:mod:`clio_agent.tools.mcp_server_instructions`). Each turn re-reads every kept-alive
server's text when it starts (:mod:`clio_agent.gact.turn_forward`), but a change made on
a plugin's page or tab (an action, or the server's own update of a resource a tab
watches) lands while a turn may already be running. So that change re-reads the text of
the server that owns it, at once, over the same kept-alive connection, and the stored
copy (``<plugin store>/server_instructions.json``) follows. Overlapping requests for one
server coalesce into one more read. Nothing here names a particular plugin or server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_STATE_KEY = "server_instructions_refreshing"
_TASKS: set[asyncio.Task[bool]] = set()


def _refreshing(app: Any) -> dict[str, bool]:
    """Namespace -> whether another read was asked for while one runs (loop thread only)."""

    state = getattr(app.state, _STATE_KEY, None)
    if not isinstance(state, dict):
        state = {}
        setattr(app.state, _STATE_KEY, state)
    return state


async def refresh_server(app: Any, namespace: str) -> bool:
    """Ask ``namespace``'s kept-alive server what it says about itself now.

    Bounded by ``tools.instructions_refresh_s`` (0 turns it off). While one read runs, a
    second request only asks for one more read after it and returns ``False`` at once.
    Returns whether the server answered; a failure keeps the recorded text.
    """

    from clio_agent.gact.external_task_connections import (  # noqa: PLC0415
        refresh_instructions,
    )
    from clio_agent.tools.mcp_server_instructions import (  # noqa: PLC0415
        instructions_refresh_s,
    )

    tasks = getattr(getattr(app, "state", None), "external_tasks", None)
    timeout = instructions_refresh_s()
    if not namespace or tasks is None or timeout <= 0:
        return False
    state = _refreshing(app)
    if namespace in state:
        state[namespace] = True
        return False
    state[namespace] = False
    answered = False
    try:
        while True:
            try:
                answered = bool(
                    await refresh_instructions(tasks, timeout=timeout, servers={namespace})
                )
            except Exception:  # noqa: BLE001 - the recorded text stays; the change stands
                logger.exception("server_instructions_refresh_failed namespace=%s", namespace)
                answered = False
            if not state.get(namespace):
                return answered
            state[namespace] = False
    finally:
        state.pop(namespace, None)


def refresh_soon(app: Any, namespace: str, loop: asyncio.AbstractEventLoop | None) -> None:
    """From any thread: start :func:`refresh_server` on ``loop`` (hand off only)."""

    if loop is None or loop.is_closed() or not namespace:
        return
    loop.call_soon_threadsafe(_start, app, namespace)


def _start(app: Any, namespace: str) -> None:
    task = asyncio.ensure_future(refresh_server(app, namespace))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


__all__ = ["refresh_server", "refresh_soon"]
