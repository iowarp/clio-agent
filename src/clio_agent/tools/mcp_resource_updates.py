"""``notifications/resources/updated`` fan-out to host listeners.

A server sends ``notifications/resources/updated`` for a URI a client watches, over a
``subscriptions/listen`` stream (:func:`clio_agent.tools.mcp_listen.watch_resource`) or
unsolicited on its connection (reported by the execution-path client's message handler,
:func:`clio_agent.tools.mcp_listen.list_changed_message_handler`). Either path reports
each one here with the namespace it arrived on; host features register a listener to react.
Listeners run on the client's event loop, so they must only hand work off (for example
with ``loop.call_soon_threadsafe``), never block or call back into the executor.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

Listener = Callable[[str, str], None]

_lock = threading.Lock()
_listeners: list[Listener] = []


def add_listener(listener: Listener) -> Callable[[], None]:
    """Call ``listener(namespace, uri)`` on every update; returns its remover."""

    with _lock:
        _listeners.append(listener)

    def remove() -> None:
        with _lock:
            if listener in _listeners:
                _listeners.remove(listener)

    return remove


def resource_updated(namespace: str, uri: str, *, via: str = "push") -> None:
    """Report one ``notifications/resources/updated`` from ``namespace``.

    ``via`` names the path it arrived on (``listen`` stream, unsolicited ``push``, or a
    ``catch_up`` after a watch reconnected), for the trace only.
    """

    trace.event("TOOLS", "mcp_resource_updated namespace=%s uri=%s via=%s", namespace, uri, via)
    with _lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(namespace, uri)
        except Exception:  # noqa: BLE001 - one listener must not starve the others
            logger.exception("resource update listener failed namespace=%s uri=%s", namespace, uri)


__all__ = ["Listener", "add_listener", "resource_updated"]
