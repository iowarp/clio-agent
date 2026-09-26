"""Timer-driven idle reap for the claude_code streaming pool.

Owner module (#775 no-accretion — a sibling of
:mod:`clio_agent.providers.claude_code_stream_bounds` rather than grown into
:mod:`clio_agent.providers.claude_code_sessions`).

**Root cause this fixes.** B1 keeps one ``ClaudeSDKClient`` (one resident
``claude`` CLI subprocess) per GACT session across that session's turns. The
idle-TTL sweep (:func:`~clio_agent.providers.claude_code_stream_bounds
.sweep_idle_session_entries`) used to run ONLY when some session asked for a
connection (``entry_for``) or a connect queued behind the process cap. Once load
stopped, nothing ever asked again, so the last sessions' CLIs stayed resident
forever: the release memory-budget gate measured three ``claude-sdk-cli``
processes (0.64 GB) still alive after a 180 s settle.

**Contract.** Every :class:`~clio_agent.providers.claude_code_sessions
.ClaudeStreamClientPool` owns one :class:`IdleSessionReaper`. The reaper is a
daemon thread that sleeps until the SOONEST idle entry reaches the configured
idle TTL (:func:`~clio_agent.providers.claude_code_stream_bounds
.session_idle_ttl_s` — the existing knob; no other timeout exists here), sweeps,
and closes every reaped client with ``disconnect()`` (the B14 teardown). It
does not poll: the pool wakes it whenever an entry is inserted or a call
finishes (:meth:`IdleSessionReaper.wake`), which are the only moments a new
idle deadline appears. With no entries left it exits, and the next wake starts
it again, so an empty pool holds no thread. A reaped session's next turn mints
a fresh entry through ``entry_for`` and reconnects transparently.

Session-open pre-connects (B2) live in the same ``pool._entries``: an unclaimed
warm client is idle from the moment its connect finishes, so the same timer
reaps it. The number of them connected at once is already bounded twice, by
``max_precede_connects`` (how many may be connecting) and by the process-wide
connect-slot cap (every connected client holds a slot).

Every timer reap is logged with the typed reason ``idle_reaped`` and
``trigger=idle_timer``, and :func:`~clio_agent.providers
.claude_code_stream_bounds.reap_idle_session_entry` emits the catalogued audit
row (``TRANSPORT_FAILURE_REASONS["idle_reaped"]``).
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import TYPE_CHECKING

from clio_agent.providers.claude_code_stream_bounds import (
    reap_idle_session_entry,
    session_idle_ttl_s,
    sweep_idle_session_entries,
)

if TYPE_CHECKING:
    from clio_agent.providers.claude_code_sessions import ClaudeStreamClientPool

logger = logging.getLogger(__name__)

__all__ = ["IdleSessionReaper", "next_idle_deadline_s"]


def next_idle_deadline_s(pool: "ClaudeStreamClientPool", ttl_s: float) -> float | None:
    """Seconds until the soonest idle entry of ``pool`` reaches ``ttl_s``.

    Returns ``None`` when no entry is idle. An entry with a call in flight has
    no deadline yet; the pool wakes the reaper when that call finishes.
    """
    soonest: float | None = None
    with pool._guard:  # noqa: SLF001 - owner-split sibling of claude_code_sessions
        entries = list(pool._entries.values())  # noqa: SLF001
    for entry in entries:
        idle = entry.idle_for()
        if idle is None:
            continue
        remaining = max(0.0, ttl_s - idle)
        if soonest is None or remaining < soonest:
            soonest = remaining
    return soonest


class IdleSessionReaper:
    """Background reaper that closes a pool's idle clients once the idle TTL passes.

    The reaper holds only a weak reference to its pool, so a discarded pool
    never stays alive because of its reaper thread.
    """

    def __init__(self, pool: "ClaudeStreamClientPool") -> None:
        self._pool_ref = weakref.ref(pool)
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._wake_pending = False
        self._stopping = False

    @property
    def running(self) -> bool:
        """Whether the reaper thread is currently alive."""
        with self._cond:
            return self._thread is not None and self._thread.is_alive()

    def wake(self) -> None:
        """Tell the reaper a new idle deadline may exist; start the thread if needed.

        Called by the pool after inserting an entry and after every call on an
        entry finishes. Cheap and non-blocking.
        """
        with self._cond:
            if self._stopping:
                return
            self._wake_pending = True
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="claude-idle-reaper", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()

    def stop(self) -> None:
        """Stop the reaper thread and wait for it to exit (pool shutdown).

        A later :meth:`wake` starts a fresh thread, so a pool that is reset and
        reused keeps being reaped.
        """
        with self._cond:
            thread = self._thread
            self._stopping = True
            self._cond.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._cond:
            if self._thread is thread:
                self._thread = None
            self._stopping = False
            self._wake_pending = False

    def _run(self) -> None:
        """Thread body: sweep, then sleep until the next deadline or a wake."""
        while True:
            with self._cond:
                if self._stopping:
                    return
                self._wake_pending = False
            pool = self._pool_ref()
            if pool is None:
                return
            ttl_s = session_idle_ttl_s()
            self._reap_once(pool, ttl_s)
            deadline = next_idle_deadline_s(pool, ttl_s)
            with pool._guard:  # noqa: SLF001 - owner-split sibling of claude_code_sessions
                has_entries = bool(pool._entries)  # noqa: SLF001
            del pool  # never pin the pool while asleep
            with self._cond:
                if self._stopping:
                    return
                if self._wake_pending:
                    continue
                if not has_entries:
                    # Nothing left to reap: exit. An insertion after the
                    # emptiness check set _wake_pending (seen above) or finds
                    # this thread gone and starts a new one.
                    self._thread = None
                    return
                self._cond.wait(timeout=deadline)

    @staticmethod
    def _reap_once(pool: "ClaudeStreamClientPool", ttl_s: float) -> None:
        """Pop and close every entry idle for at least ``ttl_s``; log each reap."""
        try:
            evicted = sweep_idle_session_entries(pool, ttl_s=ttl_s)
        except Exception:  # noqa: BLE001 - the reaper thread must survive a bad sweep
            logger.warning(
                "claude_code sdk pool: reason=idle_reap_sweep_failed trigger=idle_timer",
                exc_info=True,
            )
            return
        for session_id, entry in evicted:
            logger.info(
                "claude_code sdk pool: reason=idle_reaped trigger=idle_timer session=%s ttl_s=%.1f",
                session_id,
                ttl_s,
            )
            try:
                reap_idle_session_entry(session_id, entry)
            except Exception:  # noqa: BLE001 - one bad teardown must not strand the rest
                logger.warning(
                    "claude_code sdk pool: reason=idle_reap_close_failed session=%s",
                    session_id,
                    exc_info=True,
                )
