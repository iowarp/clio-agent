"""No-progress watchdog for the GACT turn engine (#767 Phase B).

Slice 5 of the ``turn.py`` decomposition: the per-turn cancel/watchdog wiring that
used to live inline in ``_run_turn_in_background`` as two closures
(``cancel_requested`` / ``_await_turn_work``) plus the cancel-event setup block
moves here as free functions taking
:class:`~clio_agent.gact.turn_state.TurnState` first (the gact seam convention).

The watchdog is behavior-preserving. It is a *no-progress* guard, not a hard wall:
``CLIO_GACT_TURN_TIMEOUT_S`` bounds the gap BETWEEN observable progress events
(bus publishes for this session + in-flight LM calls owned by this session), never
the total turn duration. A long-but-progressing turn runs to completion; only a
turn that goes silent for the whole window is wedged and aborted (see
[[clio-no-session-timeout]] and iowarp/clio-agent#761).

* :func:`make_turn_cancel_event` runs the former setup block: mints this turn's
  ``threading.Event``, registers it in ``app.state.cancel_events``, trips it if the
  session is already flagged for cancellation, and derives the progress timeout +
  poll cadence onto ``state``.
* :func:`cancel_requested` is the cooperative-cancel probe (``() -> bool`` when
  bound as ``partial(cancel_requested, state)``).
* :func:`await_turn_work` drives an awaitable as a task and polls for completion
  without ever disturbing a still-running turn, aborting only on a full silent
  window.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

from clio_agent.gact._params import _gact_turn_timeout_s
from clio_agent.gact.runtime.globals import _TurnTimedOut
from clio_agent.runtime.commitment_activity import (
    commitment_wait_in_flight as _commitment_wait_in_flight,
)
from clio_agent.runtime.lm_activity import lm_call_in_flight as _lm_call_in_flight

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState


def make_turn_cancel_event(state: "TurnState") -> None:
    """Mint + register this turn's cancel event and derive the watchdog cadence.

    Reproduces the former inline setup block of ``_run_turn_in_background``:
    create the ``threading.Event``, register it under this session in
    ``app.state.cancel_events``, trip it immediately if the session already
    carries a cancel flag, then derive the no-progress timeout window
    (``turn_progress_timeout_s``) and the poll cadence (``_watchdog_poll_s``)
    onto ``state``.
    """

    state.turn_cancel_event = threading.Event()
    state.app.state.cancel_events[state.sid] = state.turn_cancel_event
    if state.sid in state.app.state.cancel_flags:
        state.turn_cancel_event.set()
    # No-progress watchdog, not a hard wall: CLIO_GACT_TURN_TIMEOUT_S bounds the
    # gap BETWEEN observable progress events, never the total turn duration. A
    # long-but-progressing turn (a multi-phase EarthScope pipeline: filter ->
    # stage -> profile -> plot, each emitting bus events) must run to completion;
    # only a turn that goes silent for the whole window is wedged and aborted.
    # See [[clio-no-session-timeout]].
    state.turn_progress_timeout_s = _gact_turn_timeout_s(state.app)
    # Poll the progress heartbeat on a short cadence so abort latency after the
    # turn truly wedges stays small without busy-waiting. Cap by the window so a
    # tiny configured timeout still polls at least as often.
    state._watchdog_poll_s = (
        min(2.0, state.turn_progress_timeout_s) if state.turn_progress_timeout_s > 0 else 2.0
    )


def cancel_requested(state: "TurnState") -> bool:
    """Cooperative-cancel probe: has this turn's cancel event been tripped?

    Bind as ``partial(cancel_requested, state)`` to hand a zero-arg predicate to
    ``_cancellation_checker`` / the streamed+sync forward compat shims, exactly as
    the former closure did.
    """

    return state.turn_cancel_event.is_set()


async def await_turn_work(state: "TurnState", awaitable: Any) -> Any:
    """Await turn work under the no-progress watchdog (behavior-preserving)."""

    if state.turn_progress_timeout_s <= 0:
        return await awaitable
    # Drive the work as a task and poll for completion. asyncio.wait (unlike
    # wait_for) does NOT cancel the task when the poll interval elapses, so a
    # still-running turn is never disturbed by the watchdog tick. We seed the
    # no-progress clock at "now" so a turn that publishes nothing at all is
    # still bounded by one window; every bus publish for THIS session
    # refreshes it via EventBus.last_publish_monotonic. Progress is
    # attributed per-session on purpose: folding other sessions' publishes
    # in (the old global "" stamp) kept a genuinely wedged session alive as
    # long as any other session was busy (iowarp/clio-agent#761).
    state.bus = state.app.state.bus
    task = asyncio.ensure_future(awaitable)
    last_progress = time.monotonic()
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=state._watchdog_poll_s)
            if done:
                return task.result()
            heartbeat = state.bus.last_publish_monotonic(state.sid)
            if heartbeat > last_progress:
                last_progress = heartbeat
            # An LM call that is actively generating IS progress, even when it
            # publishes no bus events for the watchdog to see -- a deep-
            # reasoning model streams its chain-of-thought on a separate
            # channel (invisible to DSPy's answer-content listeners) and an
            # expert child runs the call synchronously in an executor (no live
            # deltas at all). Treating an in-flight LM call as progress stops
            # the watchdog from killing a working model mid-think; a per-call
            # ceiling inside lm_call_in_flight() still lets it abort a truly
            # wedged provider. See clio_agent.runtime.lm_activity.
            #
            # Scoped to THIS session (like the bus-progress stamp above): only
            # an LM call owned by this turn's session counts as its progress,
            # so a busy neighbor session's in-flight call can no longer keep a
            # genuinely wedged session alive (iowarp/clio-agent#761 defect 2).
            if _lm_call_in_flight(state.sid):
                last_progress = time.monotonic()
            # #1230: a declared wait_for_terminal commitment (#1225, unbounded
            # at the MCP-call layer) is an HONEST wait, not a stall -- treating
            # it as progress pauses the turn ceiling for exactly as long as the
            # commitment is open, so it remains a runaway backstop for turns
            # burning wall-clock OUTSIDE a commitment, never a bound on one.
            # Same per-session scoping as the LM-activity check above.
            if _commitment_wait_in_flight(state.sid):
                last_progress = time.monotonic()
            if time.monotonic() - last_progress >= state.turn_progress_timeout_s:
                state.turn_cancel_event.set()
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001,S110 - swallow during abort
                    pass
                raise _TurnTimedOut(state.turn_progress_timeout_s) from None
    except asyncio.CancelledError:
        # If the work already finished, the cancellation targeted *us* (the
        # watchdog wrapper) after the result was ready -- e.g. event-loop
        # teardown cancelling pending tasks. Surface the completed result
        # rather than masking a finished turn as a cancellation.
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is None:
                return task.result()
        task.cancel()
        raise
