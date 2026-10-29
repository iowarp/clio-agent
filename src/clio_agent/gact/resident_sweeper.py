"""The periodic sweep of the resident set: idle release without a request to trigger it.

The resident set (:mod:`clio_agent.gact.resident_ledgers`) applies its idle TTL and its
caps whenever a ledger is installed. A server nobody talks to installs nothing, so
without this sweep an idle server would keep the last conversations it served forever.
The sweeper runs :meth:`ResidentLedgerSet.sweep` on the server's event loop every
``sweep_interval_s`` and as soon as ARC pushes the set over a cap
(:meth:`ResidentSweeper.request`, callable from any thread).

After a sweep that released something, the process's free heap pages are handed back
to the operating system (``malloc_trim``), so the released bytes show up as a lower
resident size and not only as free space inside the allocator. Where the C library has
no ``malloc_trim`` the sweeper says so once (typed reason ``heap_trim_unavailable``).
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import threading
from collections.abc import Callable
from typing import Any, Optional

from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

HEAP_TRIM_UNAVAILABLE = "heap_trim_unavailable"

# Released sessions' ARC scopes are dropped off the loop just after the eviction; trim
# once those releases have had time to land.
_TRIM_DELAY_S = 2.0

__all__ = ["HEAP_TRIM_UNAVAILABLE", "ResidentSweeper", "release_free_heap"]


class ResidentSweeper:
    """Run the resident set's sweep periodically on the server loop."""

    def __init__(
        self,
        sweep: Callable[[], int],
        *,
        interval_s: float,
        loop: Callable[[], Optional[asyncio.AbstractEventLoop]],
    ) -> None:
        """Bind the sweep to the server loop.

        Args:
            sweep: The resident set's sweep; returns how many sessions it released.
            interval_s: Seconds between sweeps.
            loop: Returns the server loop once it runs (``None`` before).
        """
        self._sweep = sweep
        self._interval_s = interval_s
        self._loop = loop
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._wake: Optional[asyncio.Event] = None
        self._pending = False

    def request(self) -> None:
        """Ask for a sweep now (any thread); starts the periodic sweep if needed."""
        loop = self._loop()
        if loop is None or loop.is_closed():
            return  # before the loop runs: the next install sweeps, or the first request
        with self._lock:
            if self._pending:
                return  # one wake-up is already on its way
            self._pending = True
        try:
            loop.call_soon_threadsafe(self._on_loop)
        except RuntimeError:  # the loop closed between the check and the call
            with self._lock:
                self._pending = False

    def _on_loop(self) -> None:
        with self._lock:
            self._pending = False
        if self._task is None or self._task.done():
            self._wake = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="clio-resident-sweeper"
            )
        assert self._wake is not None
        self._wake.set()

    async def _run(self) -> None:
        wake = self._wake
        assert wake is not None
        while True:
            try:
                await asyncio.wait_for(wake.wait(), timeout=self._interval_s)
            except TimeoutError:
                pass
            wake.clear()
            try:
                released = self._sweep()
            except Exception:  # noqa: BLE001 - logged and traced; the next tick retries
                logger.exception("resident sweep failed reason=resident_sweep_failed")
                trace.event("RESIDENT-LEDGER", "sweep_failed reason=resident_sweep_failed")
                continue
            if released:
                trace.event("RESIDENT-LEDGER", "swept released=%s", released)
                await asyncio.sleep(_TRIM_DELAY_S)
                await asyncio.to_thread(release_free_heap)


_TRIM: dict[str, Any] = {}


def release_free_heap() -> bool:
    """Return the C allocator's free pages to the OS; ``False`` where unavailable."""
    if "fn" not in _TRIM:
        name = ctypes.util.find_library("c")
        try:
            fn = getattr(ctypes.CDLL(name), "malloc_trim", None) if name else None
        except OSError:
            fn = None
        _TRIM["fn"] = fn
        if fn is None:
            trace.event("RESIDENT-LEDGER", "heap_trim reason=%s", HEAP_TRIM_UNAVAILABLE)
    fn = _TRIM["fn"]
    if fn is None:
        return False
    fn(0)
    return True
