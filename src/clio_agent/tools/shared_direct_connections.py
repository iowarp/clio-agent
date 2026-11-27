"""Let an executor hold the application's kept-alive direct connection to a server.

A declared server routed direct has one kept-alive connection per fleet, owned by the
application (``gact.external_task_connections``): tool calls lease it one invocation
at a time, and the executor's own uses of the namespace (listing, resource reads and
watches, the listen stream) hold it. One server process then serves the whole fleet
instead of one process per call.

The application registers its holder for its event loop with :func:`register`; an
executor connecting a direct namespace on that loop asks :func:`holder`. Without one
(the CLI, a test without an application) the executor keeps its own connection.

This module only carries the hook; it owns no connection.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

#: ``hold(server, auth_scope, owner, configured)`` returns the connected client and the
#: coroutine function that releases the hold. ``owner`` is the fleet's namespace proxy;
#: ``configured`` is a fresh, unentered direct client, used only when nothing is open.
Holder = Callable[[str, str, Any, Any], Awaitable[tuple[Any, Callable[[], Awaitable[None]]]]]

_LOCK = threading.Lock()
_HOLDERS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[Holder, Callable[[], bool]]
] = weakref.WeakKeyDictionary()


def register(
    loop: asyncio.AbstractEventLoop, hold: Holder, available: Callable[[], bool]
) -> Callable[[], None]:
    """Register ``hold`` for executors on ``loop`` while ``available()``; return its removal."""

    entry = (hold, available)
    with _LOCK:
        _HOLDERS[loop] = entry

    def remove() -> None:
        with _LOCK:
            if _HOLDERS.get(loop) is entry:
                del _HOLDERS[loop]

    return remove


def holder() -> Holder | None:
    """The holder registered for the running loop, if any and still available."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    with _LOCK:
        entry = _HOLDERS.get(loop)
    if entry is None or not entry[1]():
        return None
    return entry[0]


class HeldConnection:
    """An executor's namespace context for a held connection: exiting releases it."""

    def __init__(self, release: Callable[[], Awaitable[None]]) -> None:
        self._release: Callable[[], Awaitable[None]] | None = release

    async def __aexit__(self, *exc: object) -> None:
        release, self._release = self._release, None
        if release is not None:
            await release()


__all__ = ["HeldConnection", "Holder", "holder", "register"]
