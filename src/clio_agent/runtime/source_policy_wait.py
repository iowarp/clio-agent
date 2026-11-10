"""Bounded wait for a live connected-data access change (a grant swap).

Linking a folder swaps the workspace fleets' grants live under the host's
``_source_policy_changing`` flag. A turn the person starts in that window waits
for the swap and then runs, instead of failing its first tool calls.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

#: Longest a fleet lease or tool resolve waits for a connected-data access change
#: (a live grant swap restarting the workspace fleets) before it gives up.
SOURCE_POLICY_WAIT_S = 30.0


def _on_event_loop() -> bool:
    """True on a thread running an asyncio loop, which must never sleep-wait."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def await_source_policy_settled(host: Any, lock: threading.Lock, *, wait: bool = True) -> None:
    """With ``lock`` held, wait (bounded) while ``host`` is changing connected-data access.

    Callers that must not block (residency holds, an event-loop thread) are refused
    at once. The lock is released while sleeping so the swap can finish, and is held
    again on return.
    """
    if not getattr(host, "_source_policy_changing", False):
        return
    if not wait or _on_event_loop():
        raise RuntimeError("CLIO is updating connected-data access; retry after setup finishes")
    deadline = time.monotonic() + SOURCE_POLICY_WAIT_S
    while getattr(host, "_source_policy_changing", False):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"CLIO is still updating connected-data access after "
                f"{SOURCE_POLICY_WAIT_S:g}s; retry once setup finishes"
            )
        lock.release()
        try:
            time.sleep(0.05)
        finally:
            lock.acquire()
