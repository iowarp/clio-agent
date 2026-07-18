"""Own the lifetime of in-flight GACT turn tasks, decoupled from the request
that submitted them (#662, campaign #948 S1).

A GACT turn is a long-lived (seconds-to-minutes) background job: the POST that
submits it acks immediately and the turn runs on afterward, streaming progress
over SSE. Historically each turn was a bare ``asyncio.create_task`` on whatever
loop the submitting request happened to run on, tracked only in a per-session
``app.state.in_flight_turns[sid]`` slot. Three concrete failure modes followed:

* **GC-cancellation.** ``asyncio`` keeps only a *weak* reference to a bare task;
  the sole strong reference was the ``in_flight_turns[sid]`` slot. A second POST
  to the same session *overwrote* that slot (see the busy gate below), dropping
  the first turn's last strong ref — the event loop was then free to garbage-
  collect and cancel a still-running turn ("Task was destroyed but it is
  pending"). This is a direct contributor to the #662 flakiness (steady mid-turn
  work — the default-on semantic-trace writer — widens the window).
* **Request-teardown races (#662).** A turn anchored to a transient request /
  anyio-portal context can be torn down when that context finishes rather than
  when the *app* stops.
* **No deterministic drain.** Lifespan shutdown cancelled the MCP / scheduler
  background tasks but never touched ``in_flight_turns`` — turns were abandoned
  when the loop stopped (zombies, lost persistence, no typed reason).

:class:`TurnRunner` is the single owner that fixes all three. It holds a **master
strong-ref set** of every live turn task (so no turn is ever GC-cancelled,
regardless of what happens to the per-session slot), schedules every turn on the
**app-lifetime loop captured at startup** (never a request's transient loop), and
:meth:`drain` gives lifespan shutdown a deterministic, typed teardown. It also
exposes :meth:`busy` — the within-session concurrency signal the POST route gates
on so a second concurrent turn can no longer silently overwrite the first.

The runner keeps ``app.state.in_flight_turns`` populated as its per-session view;
existing readers (the ``/cancel`` route, session status projection) are unchanged.
Background children (#948 S3) will spawn through this same owner on a dedicated
executor, so the drain guarantee extends to them for free.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Typed reason recorded on every turn the runner cancels during shutdown drain —
# distinguishes a deliberate teardown from a user /cancel or a request abort so
# the trace never shows an unexplained cancellation (no-silent-fallback rule).
DRAIN_REASON_SERVER_SHUTDOWN = "server_shutdown"

# Typed error code returned by the within-session busy gate. The session already
# has a turn in flight; a second concurrent turn is refused rather than silently
# overwriting the first (which orphaned it, uncancellable, both writing the same
# session + ARC).
BUSY_ERROR_CODE = "session_busy"

# Bounded number of drain re-snapshot passes (defense against a producer that
# keeps spawning turns during shutdown; producers are quiesced first, so one pass
# is the norm).
_MAX_DRAIN_PASSES = 5


@dataclass(frozen=True)
class TurnHandle:
    """Lightweight record of a live turn, kept alongside its task so the busy
    gate can return a truthful 409 (which turn is running, since when)."""

    sid: str
    turn_id: str
    started_at: float


@dataclass(frozen=True)
class DrainOutcome:
    """Typed summary of a shutdown drain, mirroring the ``*Result`` dataclasses
    in :mod:`clio_agent.runtime.process_tree` (no silent teardown)."""

    total: int
    settled: int  # finished cooperatively within the grace window
    hard_cancelled: int  # forced with task.cancel() after the grace window
    reason: str

    @property
    def clean(self) -> bool:
        """True when every in-flight turn settled without a forced cancel."""

        return self.hard_cancelled == 0


class TurnRunner:
    """Single owner of in-flight turn-task lifetime for one GACT app.

    Construct once in ``build_app`` with the app's ``in_flight_turns`` dict as the
    per-session view, call :meth:`bind_loop` from the lifespan startup to anchor
    to the serving loop, and route every turn through :meth:`spawn`.
    """

    def __init__(self, in_flight_turns: dict[str, "asyncio.Task[object]"]) -> None:
        # The per-session view shared with the rest of the app (``/cancel`` etc.).
        self._in_flight = in_flight_turns
        # Master strong references to EVERY live turn — the GC-cancellation fix.
        # Keyed by nothing: a turn stays here until it actually finishes, even if
        # its per-session slot is replaced.
        self._all: set["asyncio.Task[object]"] = set()
        self._handles: dict[str, TurnHandle] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_session_idle: Callable[[str], None] | None = None

    def set_idle_hook(self, hook: Callable[[str], None] | None) -> None:
        """Register a callback fired (on the event loop) when a session's turn slot
        clears — i.e. the session just became free. Used to re-drive work deferred
        because the session was busy (e.g. an ask-user resume that couldn't stage
        while an intervening turn ran). The hook must not raise; errors are logged."""

        self._on_session_idle = hook

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the app-lifetime event loop (call once, from lifespan startup
        before ``yield``). Turns are scheduled on THIS loop, never on whatever
        transient loop a submitting request runs on."""

        self._loop = loop

    def busy(self, sid: str) -> bool:
        """True iff a turn is currently in flight for ``sid`` — the within-session
        concurrency signal the POST route gates on."""

        task = self._in_flight.get(sid)
        return task is not None and not task.done()

    def active_count(self) -> int:
        """Number of turn tasks currently in flight across all sessions (the
        master-set size, excluding any that have finished but not yet run their
        done-callback). Feeds doctor/metrics and the drain assertions."""

        return sum(1 for task in self._all if not task.done())

    def handle(self, sid: str) -> TurnHandle | None:
        """The live turn's handle for ``sid`` (for the busy-gate 409 body), or
        ``None`` when idle."""

        if not self.busy(sid):
            return None
        return self._handles.get(sid)

    def spawn(
        self,
        coro: "asyncio.coroutines.Coroutine[object, object, object]",
        *,
        sid: str,
        turn_id: str,
    ) -> "asyncio.Task[object]":
        """Schedule ``coro`` as an owned turn task.

        Registers the task in the master set (strong ref — no GC-cancellation)
        and the per-session view, records a :class:`TurnHandle`, and wires a
        done-callback that removes it from both on completion.
        """

        loop = self._loop or asyncio.get_event_loop()
        task = loop.create_task(coro)
        self._all.add(task)
        self._in_flight[sid] = task
        self._handles[sid] = TurnHandle(sid=sid, turn_id=turn_id, started_at=time.time())

        def _done(finished: "asyncio.Task[object]", _sid: str = sid) -> None:
            self._all.discard(finished)
            # Only clear the per-session slot/handle if THIS task still owns it —
            # a later turn may already have replaced it (though the busy gate now
            # makes that path unreachable for user POSTs, background children can
            # legitimately reuse a session slot across sequential runs).
            slot_cleared = False
            if self._in_flight.get(_sid) is finished:
                self._in_flight.pop(_sid, None)
                self._handles.pop(_sid, None)
                slot_cleared = True
            # The session just became free — re-drive anything deferred because it
            # was busy (e.g. an ask-user resume). Guard on not-busy so a fast
            # sequential reuse doesn't fire the hook while another turn holds the slot.
            if slot_cleared and self._on_session_idle is not None and not self.busy(_sid):
                try:
                    self._on_session_idle(_sid)
                except Exception:  # noqa: BLE001 - a hook error must not break task cleanup
                    logger.exception("turn-runner idle hook failed for session %s", _sid)

        task.add_done_callback(_done)
        return task

    async def drain(
        self,
        *,
        grace: float = 5.0,
        cancel_signal: Callable[[str], None] | None = None,
        reason: str = DRAIN_REASON_SERVER_SHUTDOWN,
    ) -> DrainOutcome:
        """Deterministically settle every in-flight turn for shutdown.

        First fires the cooperative cancel signal (the existing per-session
        ``cancel_flags``/``cancel_events`` machinery, passed in as
        ``cancel_signal``) so turns that check between boundaries settle
        themselves with a truthful cancelled envelope. Waits up to ``grace``
        seconds, then hard-cancels any straggler and awaits its
        ``CancelledError`` so no pending task is left when the loop stops.

        Returns a typed :class:`DrainOutcome`; the caller logs it so a forced
        cancel is never silent.
        """

        # Re-snapshot each pass: a turn spawned DURING the drain (a producer that
        # has not been quiesced yet — e.g. a scheduler tick firing in the grace
        # window) would otherwise escape a single snapshot and be left running as
        # the rest of teardown proceeds. Callers should quiesce producers first;
        # this loop is defense-in-depth. Bounded so a runaway producer can't spin
        # shutdown forever.
        total = 0
        settled = 0
        hard_cancelled = 0
        seen: set[asyncio.Task[object]] = set()
        for pass_index in range(_MAX_DRAIN_PASSES):
            batch = [t for t in self._all if not t.done() and t not in seen]
            if not batch:
                break
            seen.update(batch)
            total += len(batch)

            if cancel_signal is not None:
                for sid in list(self._handles):
                    if self.busy(sid):
                        cancel_signal(sid)

            # Give cooperative cancellation the grace window on the first pass;
            # tasks born mid-drain (later passes) are settled immediately.
            await asyncio.wait(batch, timeout=max(grace, 0.0) if pass_index == 0 else 0.0)

            stragglers = [t for t in batch if not t.done()]
            for task in stragglers:
                task.cancel()
                hard_cancelled += 1
            if stragglers:
                # Await the forced cancellations so the loop never stops with a
                # pending task (the zombie the old shutdown left behind).
                await asyncio.gather(*stragglers, return_exceptions=True)
            settled += len(batch) - len(stragglers)

        return DrainOutcome(
            total=total,
            settled=settled,
            hard_cancelled=hard_cancelled,
            reason=reason,
        )


def session_busy_error_payload(runner: "TurnRunner | None", sid: str) -> dict | None:
    """The typed 409 body for the within-session busy gate, or ``None`` when the
    session is idle.

    Shared by EVERY HTTP turn producer (POST /messages, retry) so they refuse a
    concurrent turn identically — the gate is a property of the session slot, not
    of one route (the scheduler, a non-HTTP producer, gates via ``busy()`` + skip).
    """

    if runner is None or not runner.busy(sid):
        return None
    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo  # noqa: PLC0415 - avoid import cycle

    handle = runner.handle(sid)
    return ErrorEnvelope(
        error=ErrorInfo(
            error=BUSY_ERROR_CODE,
            message=(
                "a turn is already running on this session; wait for it to finish "
                "(or POST /cancel) before sending another message"
            ),
            details={
                "session_id": sid,
                "running_turn_id": handle.turn_id if handle else "",
                "running_since": handle.started_at if handle else None,
                "recovery_actions": ["wait", "cancel", "retry"],
            },
            recoverable=True,
        )
    ).model_dump(exclude_none=True)


def install_turn_runner(app: "FastAPI") -> TurnRunner:
    """Create the app's :class:`TurnRunner` over ``app.state.in_flight_turns`` and
    stash it on ``app.state.turn_runner``. Call once from ``build_app`` after the
    ``in_flight_turns`` dict exists. Owner-module entry point so ``build_app``
    grows by one line, not the runner's guts (anti-accretion)."""

    runner = TurnRunner(app.state.in_flight_turns)
    app.state.turn_runner = runner
    return runner


async def drain_app_turns(app: "FastAPI", logger: logging.Logger) -> DrainOutcome:
    """Drain in-flight turns for lifespan shutdown, wiring the runner to the app's
    existing cooperative-cancel machinery (``cancel_flags`` / ``cancel_events``).
    Stores the typed outcome on ``app.state.turn_drain_outcome`` (a forced cancel
    is a recorded fact, not only a log line) and logs any non-empty drain."""

    def _cancel_signal(sid: str) -> None:
        app.state.cancel_flags.add(sid)
        event = app.state.cancel_events.get(sid)
        if event is not None:
            event.set()

    outcome = await app.state.turn_runner.drain(cancel_signal=_cancel_signal)
    app.state.turn_drain_outcome = outcome
    if outcome.total:
        logger.info(
            "turn drain on shutdown: %d in-flight, %d settled, %d hard-cancelled (%s)",
            outcome.total,
            outcome.settled,
            outcome.hard_cancelled,
            outcome.reason,
        )
    return outcome
