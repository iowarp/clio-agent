"""The browser service's event loop, which starts Playwright's driver in its own session.

Stopping CLIO (``clio stop``, the cloud stack, Ctrl-C in a terminal) signals the server's
whole process group. Playwright's driver is a child process in that group, so it died with
the signal before the server's shutdown could close its pages: the shutdown failed on a
closed connection, and with the ``cdp`` backend CLIO's tabs stayed open in the person's
Chrome. Started in its own session, the driver hears no group signal; the server closes
the pages and browsers itself. The driver still ends with the server whatever way the
server ends, because it exits when its stdin (a pipe from the server) closes, and it
closes the browsers it launched as it goes.

Only Playwright starts subprocesses on this loop. Windows has no process-group signals,
so there the ordinary loop is used.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

__all__ = ["driver_loop"]


def driver_loop() -> asyncio.AbstractEventLoop:
    """A new event loop for the browser thread."""
    if os.name == "nt":
        return asyncio.new_event_loop()
    return _OwnSessionLoop()


class _OwnSessionLoop(asyncio.SelectorEventLoop):
    """A selector loop whose subprocesses start in a new session."""

    async def subprocess_exec(  # type: ignore[override]
        self, protocol_factory: Any, *args: Any, **kwargs: Any
    ) -> Any:
        kwargs.setdefault("start_new_session", True)
        return await super().subprocess_exec(protocol_factory, *args, **kwargs)
