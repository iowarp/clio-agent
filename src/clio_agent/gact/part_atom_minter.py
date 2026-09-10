"""Per-turn transcript minter: the FIFO that persists a session's transcript atoms off
the loop thread (#1334, the streaming-native persistence of #1337).

Why: every transcript persist is a synchronous ARC store RPC. Issued from the loop thread
(the user message minted inside ``POST /messages`` before its response, a steer appended
mid-turn) each one froze the server for its duration. This module owns ONE consumer thread
per turn that drains persist jobs in order, so the loop never waits on the store and the
atoms of one session land in append order (the lane grouping relies on it).

Contract:

* :func:`open_turn_minter` / :func:`close_turn_minter` bracket a turn (opened in the
  turn's off-loop setup, closed when the transcript settles).
* :func:`run_transcript_job` is the ONE entry for deferred transcript persistence: the
  open minter takes the job; without one (a steer landing before the turn opened its
  minter) the job is dispatched off the loop with a typed audit on failure.
* :meth:`PartAtomMinter.barrier` is the finalize gate: it drains the queue and re-runs
  every failed job inline on the caller's (executor) thread; a job that still fails
  raises, so the must-succeed contract of ``transcript_projection.on_message_appended``
  (a failed mint fails the turn, never a half-committed transcript) is unchanged, only
  moved to the finalize boundary.

Deliberately a dedicated thread, not the shared depth-keyed agent-task executor: a slot
parked in ~90 ms RPCs for a whole turn would starve child forwards at the same depth.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Optional

from clio_agent.gact.off_loop import schedule_off_loop
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

TRANSCRIPT_JOB_FAILED = "transcript_job_failed"
MINTER_DRAIN_TIMEOUT = "transcript_minter_drain_timeout"

_REGISTRY_LOCK = threading.Lock()
_STOP = object()


class PartAtomMinter:
    """One FIFO consumer thread that persists transcript jobs for one turn."""

    def __init__(self, *, session_id: str, turn_id: str) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._failures: list[tuple[str, Callable[[], Any], BaseException]] = []
        self._lock = threading.Lock()
        self._closed = False
        self._pending = 0
        self._idle = threading.Condition(self._lock)
        self._thread = threading.Thread(
            target=self._run, name=f"clio-atom-mint-{session_id}", daemon=True
        )
        self._thread.start()

    # ---- producer side --------------------------------------------------------

    def enqueue(self, label: str, fn: Callable[[], Any]) -> bool:
        """Queue ``fn`` (non-blocking). ``False`` once the minter is closed."""

        with self._lock:
            if self._closed:
                return False
            self._pending += 1
        self._queue.put((label, fn))
        return True

    # ---- consumer side --------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            label, fn = item
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - recorded; the barrier re-raises
                stream_audit(
                    "transcript.job_failed",
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    label=label,
                    reason=TRANSCRIPT_JOB_FAILED,
                    error=type(exc).__name__,
                    message=str(exc)[:300],
                )
                logger.error(
                    "transcript job %s failed for session=%s (%s); the finalize barrier retries",
                    label,
                    self.session_id,
                    TRANSCRIPT_JOB_FAILED,
                    exc_info=True,
                )
                with self._lock:
                    self._failures.append((label, fn, exc))
            finally:
                with self._idle:
                    self._pending -= 1
                    if self._pending == 0:
                        self._idle.notify_all()

    # ---- finalize side --------------------------------------------------------

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait until every queued job ran. ``False`` on timeout (audited)."""

        with self._idle:
            done = self._idle.wait_for(lambda: self._pending == 0, timeout=timeout)
        if not done:
            stream_audit(
                "transcript.minter_drain_timeout",
                session_id=self.session_id,
                turn_id=self.turn_id,
                pending=self._pending,
                reason=MINTER_DRAIN_TIMEOUT,
            )
            logger.error(
                "transcript minter drain timed out session=%s pending=%d (%s)",
                self.session_id,
                self._pending,
                MINTER_DRAIN_TIMEOUT,
            )
        return done

    def barrier(self, *, timeout: float = 5.0) -> None:
        """Drain, then re-run every failed job inline; a job that fails again raises.

        Called on the finalize executor thread, right before the assistant message is
        persisted, so the turn's transcript is complete-or-failed at one boundary.
        """

        self.drain(timeout=timeout)
        with self._lock:
            failed = list(self._failures)
            self._failures.clear()
        for label, fn, first_exc in failed:
            logger.warning(
                "transcript barrier: retrying failed job %s (first error: %s)",
                label,
                type(first_exc).__name__,
            )
            fn()  # raises through the finalize envelope when it fails again

    def close(self, *, timeout: float = 5.0) -> None:
        """Stop accepting jobs, drain what is queued, stop the thread."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.drain(timeout=timeout)
        self._queue.put(_STOP)
        self._thread.join(timeout=timeout)
        with self._lock:
            leftover = list(self._failures)
        if leftover:
            logger.error(
                "transcript minter closed with %d unrecovered job(s) session=%s labels=%s",
                len(leftover),
                self.session_id,
                [label for label, _fn, _exc in leftover],
            )


# ---- registry on app.state ----------------------------------------------------------


def _registry(app: Any) -> dict[str, PartAtomMinter]:
    with _REGISTRY_LOCK:
        reg = getattr(app.state, "turn_minters", None)
        if reg is None:
            reg = {}
            app.state.turn_minters = reg
        return reg


def open_turn_minter(app: Any, session_id: str, turn_id: str) -> PartAtomMinter:
    """Open (or replace) the session's minter for this turn."""

    minter = PartAtomMinter(session_id=session_id, turn_id=turn_id)
    with _REGISTRY_LOCK:
        reg = getattr(app.state, "turn_minters", None)
        if reg is None:
            reg = {}
            app.state.turn_minters = reg
        previous = reg.get(session_id)
        reg[session_id] = minter
    if previous is not None:
        previous.close()
    return minter


def turn_minter(app: Any, session_id: str) -> Optional[PartAtomMinter]:
    """The session's open minter, or ``None``."""

    return _registry(app).get(session_id)


def close_turn_minter(app: Any, session_id: str) -> None:
    """Close and drop the session's minter (no-op when none is open)."""

    with _REGISTRY_LOCK:
        reg = getattr(app.state, "turn_minters", None)
        minter = reg.pop(session_id, None) if reg is not None else None
    if minter is not None:
        minter.close()


def persist_finalized_message(app: Any, session_id: str, message: Any) -> None:
    """Finalize's persist: the barrier (every deferred job landed or raised), then the
    assistant message through the ``clio_agent.gact.app`` seam (bound at call time so
    the #714 test monkeypatches keep intercepting). Runs on the finalize executor."""

    from clio_agent.gact.app import _append_session_message  # noqa: PLC0415

    minter = turn_minter(app, session_id)
    if minter is not None:
        minter.barrier()
    _append_session_message(app, session_id, message)


def run_transcript_job(app: Any, session_id: str, label: str, fn: Callable[[], Any]) -> None:
    """Persist a transcript job off the loop thread.

    Order of preference: the session's open minter (FIFO, covered by the finalize
    barrier); else :func:`schedule_off_loop` (inline without a loop on this thread,
    otherwise dispatched with a typed audit on failure).
    """

    minter = turn_minter(app, session_id)
    if minter is not None and minter.enqueue(label, fn):
        return
    schedule_off_loop(fn, label=label)


__all__ = [
    "MINTER_DRAIN_TIMEOUT",
    "TRANSCRIPT_JOB_FAILED",
    "PartAtomMinter",
    "close_turn_minter",
    "open_turn_minter",
    "persist_finalized_message",
    "run_transcript_job",
    "turn_minter",
]
