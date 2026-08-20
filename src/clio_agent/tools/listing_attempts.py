"""In-flight discovery/listing attempt ownership registry (#1240).

The child-process leak this module fixes: ``tools.gateway._list_declared_tools``
builds its OWN fastmcp ``Client`` per attempt and always tore it down in a
``finally`` -- correctly -- but only on that SAME coroutine's own path back
out. Nothing else could reach in and close a DIFFERENT thread's attempt, so a
caller that gave up waiting (``tools.mcp_discovery.discover_declared_tools_bounded``'s
per-namespace deadline, or ``ClioAgent.shutdown``) left the connect/list call
— and its spawned stdio child — running for as long as that call took, which
(pre-#1240) could be forever: neither ``mcp_probe_hardening`` nor the SDK's
own per-request timeout bounded ``list_tools``/a legacy ``initialize``
fallback by default.

This registry is the ownership half of the fix (the other half,
``gateway._list_declared_tools`` now forwarding a real ``timeout_s``, bounds
the call itself so it can never hang forever even with nobody watching):
every attempt that supplies an ``attempt_key`` registers its own event loop +
``Client`` here for the duration of the call, so :func:`force_close_listing_attempt`
can close that SPECIFIC attempt's transport from ANOTHER thread via
``asyncio.run_coroutine_threadsafe`` -- the sanctioned cross-thread asyncio
primitive, never raw Transport/Protocol poking. Keyed by an opaque per-call
token, never a namespace name: a healer re-probe and the stale attempt it is
replacing can be in flight for the SAME namespace at once, and each must be
individually closeable.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from typing import Any

_lock = threading.Lock()
_attempts: dict[object, tuple[asyncio.AbstractEventLoop, Any]] = {}


def register(attempt_key: object | None, loop: asyncio.AbstractEventLoop, client: Any) -> None:
    """Record ``client`` (owned by ``loop``) as the live attempt for ``attempt_key``.

    A ``None`` key (the caller opted out of ownership tracking) is a no-op.
    """

    if attempt_key is None:
        return
    with _lock:
        _attempts[attempt_key] = (loop, client)


def unregister(attempt_key: object | None) -> None:
    """Drop ``attempt_key``'s registration (the attempt is finishing on its own)."""

    if attempt_key is None:
        return
    with _lock:
        _attempts.pop(attempt_key, None)


def force_close_listing_attempt(attempt_key: object, *, wait_s: float = 5.0) -> bool:
    """Force-close attempt ``attempt_key`` from ANOTHER thread.

    Schedules the same ``client.transport.disconnect()`` the attempt's own
    ``finally`` runs, onto the attempt's OWN loop. A missing/already-finished
    attempt, or a loop that closed on its own between the caller deciding to
    abandon it and this call, is a no-op -- never raises.

    Returns:
        True when a live attempt was found and its close was requested (the
        close itself is always best-effort/suppressed-on-exception, same as
        the attempt's own teardown); False when there was nothing to close.
    """

    with _lock:
        entry = _attempts.get(attempt_key)
    if entry is None:
        return False
    loop, client = entry

    async def _close() -> None:
        disconnect = getattr(client.transport, "disconnect", None)
        if disconnect is not None:
            with suppress(Exception):
                await disconnect()

    try:
        future = asyncio.run_coroutine_threadsafe(_close(), loop)
    except RuntimeError:
        return False  # the loop already closed -- the attempt finished on its own
    with suppress(Exception):
        future.result(timeout=wait_s)
    return True


def force_close_all(*, wait_s: float = 5.0) -> int:
    """Force-close every currently-registered attempt (shutdown symmetry). Returns the count closed."""

    with _lock:
        keys = list(_attempts)
    return sum(1 for key in keys if force_close_listing_attempt(key, wait_s=wait_s))


__all__ = [
    "force_close_all",
    "force_close_listing_attempt",
    "register",
    "unregister",
]
