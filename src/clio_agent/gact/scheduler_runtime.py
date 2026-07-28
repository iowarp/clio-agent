"""Scheduler tick + fire runtime for the GACT server (#21, #766, #1081).

Owner module for the background scheduler loop, extracted from ``gact/app.py`` (the
no-accretion ground rule: app.py is at its ratchet baseline). It hosts the four
helpers the boot-time ``_lifespan`` and the scheduler tests use:

* :func:`_scheduler_tick` — the once-a-minute loop, boundary-aligned to avoid drift;
* :func:`_scheduler_tick_once` — process one tick: retry deferred, then fire due;
* :func:`_fire_schedule` — stage one due schedule as a background user turn;
* :func:`_seconds_until_next_minute` — the boundary-aligned inter-tick sleep.

``app.py`` re-exports all four so ``from clio_agent.gact.app import _scheduler_tick``
(and the ``monkeypatch.setattr(gact_app, "_turn_start_background_user_turn", ...)`` the
tests use) keep working: the turn-staging call is resolved through the app module at
fire time, not bound at import.

P4.3 additions over the original: an explicit per-schedule **overlap policy**
(``queue`` = the existing deferred-not-dropped behaviour; ``skip`` = drop the occurrence
and advance, with a typed reason), and **retry/backoff** on a staging failure
(``ScheduleStore.record_fire_failure`` — deferred-not-dropped, typed, never silent).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _seconds_until_next_minute(now: datetime) -> float:
    """Seconds from ``now`` to just past the next UTC minute boundary.

    Aligning the inter-tick sleep to the boundary (instead of a flat ``sleep(60)``
    after processing) keeps slow ticks from drifting past cron minutes (#766). The
    small epsilon lands the wake *after* the boundary so a due ``next_fire_at`` on the
    minute is seen; the floor guards against a zero/negative sleep hot loop right at
    the boundary."""

    remaining = 60.0 - (now.second + now.microsecond / 1_000_000.0)
    return max(0.5, remaining + 0.05)


def _fire_schedule(app: "FastAPI", sch: Any) -> None:
    """Fire one due schedule through the standard user-turn staging.

    Stages the question via the same ``_start_background_user_turn`` engine POST
    /messages uses (#766) — the user message is persisted + published, the session
    flips to ``running``, and the turn task is registered in
    ``app.state.in_flight_turns`` so cancellation can reach it — then marks the
    schedule fired once staging has actually succeeded.

    A schedule pointing at a missing session is logged with a typed reason. When the
    session is BUSY the overlap policy decides: ``queue`` (default) defers the
    occurrence for retry (a coarse cron won't re-match next minute, so relying on
    ``due_now`` would silently lose it); ``skip`` drops this occurrence and advances to
    the next fire, with a typed ``schedule_overlap_skipped`` reason.

    ``mark_fired`` is called AFTER staging succeeds, not before (#1031 P4.3 adversarial
    review fix): staging can raise synchronously, and ``mark_fired`` mutates the store
    unconditionally — it POPS a one-shot row outright and increments/advances a
    recurring row's ``fire_count``/``next_fire_at``. Marking before staging meant a
    staging exception left ``_fire_one``'s ``record_fire_failure`` retry either a no-op
    (the one-shot row was already gone, so the occurrence was silently dropped with no
    retry) or counting an occurrence that never ran (a recurring row's ``max_fires``
    ceiling could retire it after fewer real executions than requested). Marking only
    on success — still synchronously, within this same call, with no ``await`` in
    between — also preserves the same-tick no-double-fire invariant
    ``_scheduler_tick_once`` relies on: its deferred-retry loop runs to completion
    (including this mark, for anything it successfully fires) before ``due_now()`` is
    queried, so a schedule fired via the retry path can't reappear in the due-scan."""

    sess = app.state.sessions.get(sch.session_id)
    if sess is None:
        app.state.schedules.mark_fired(sch.id)
        logger.warning(
            "scheduler tick error reason=schedule_session_not_found schedule_id=%s session_id=%s",
            sch.id,
            sch.session_id,
        )
        return
    # #948 S1: the within-session busy gate applies to EVERY turn producer. A due
    # schedule whose session already has a turn in flight must NOT double-stage a
    # concurrent turn (which would orphan the running one).
    if app.state.turn_runner.busy(sch.session_id):
        policy = str(getattr(sch, "overlap_policy", "queue") or "queue")
        if policy == "skip":
            # Drop THIS occurrence and advance to the next fire (mark_fired recomputes
            # next_fire_at). The skip is typed, never silent.
            app.state.deferred_schedules.discard(sch.id)
            app.state.schedules.mark_fired(sch.id)
            logger.info(
                "scheduler tick skipped reason=schedule_overlap_skipped "
                "schedule_id=%s session_id=%s",
                sch.id,
                sch.session_id,
            )
            return
        # queue: DEFER (do not drop). Record the id; _scheduler_tick_once retries it
        # every tick until the session frees. Left UNMARKED so the state stays truthful.
        app.state.deferred_schedules.add(sch.id)
        logger.info(
            "scheduler tick deferred reason=schedule_session_busy schedule_id=%s session_id=%s",
            sch.id,
            sch.session_id,
        )
        return
    app.state.deferred_schedules.discard(sch.id)
    # Resolve the turn-staging engine through the app module at fire time so tests can
    # monkeypatch ``clio_agent.gact.app._turn_start_background_user_turn``.
    from clio_agent.gact import app as _app_mod  # noqa: PLC0415

    _app_mod._turn_start_background_user_turn(
        app,
        sch.session_id,
        sess,
        sch.question,
        metadata={"scheduled": True, "schedule_id": sch.id},
        prev_status=str(getattr(sess, "status", "idle") or "idle"),
    )
    # Only count the occurrence once staging actually succeeded — see the docstring
    # above for why this must run AFTER, not before, the staging call.
    app.state.schedules.mark_fired(sch.id)


def _scheduler_tick_once(app: "FastAPI") -> None:
    """Process one scheduler tick: retry any deferred schedule, then fire every due one.

    Never raises: a due-scan failure or a per-schedule firing failure is logged with a
    typed reason (``schedule_due_scan_failed`` / ``schedule_fire_failed``) so failures
    are visible instead of silently swallowed, and one bad schedule cannot starve the
    rest. A staging failure additionally records a retry (deferred-not-dropped) so the
    occurrence is not lost."""

    # Retry any schedule deferred because its session was busy at its fire time.
    deferred = getattr(app.state, "deferred_schedules", None)
    if deferred:
        for sched_id in list(deferred):
            sch = app.state.schedules.get(sched_id)
            if sch is None:
                deferred.discard(sched_id)
                continue
            if app.state.turn_runner.busy(sch.session_id):
                continue  # still busy — keep deferred, retry next tick
            deferred.discard(sched_id)
            _fire_one(app, sch)

    try:
        now = datetime.now(timezone.utc)
        due = list(app.state.schedules.due_now(now))
    except Exception:  # noqa: BLE001 - the tick loop must survive a bad store
        logger.warning("scheduler tick error reason=schedule_due_scan_failed", exc_info=True)
        return
    for sch in due:
        _fire_one(app, sch)


def _fire_one(app: "FastAPI", sch: Any) -> None:
    """Fire one schedule, logging + retrying (deferred-not-dropped) on a staging failure."""

    try:
        _fire_schedule(app, sch)
    except Exception as exc:  # noqa: BLE001 - one bad schedule must not kill the loop
        logger.warning(
            "scheduler tick error reason=schedule_fire_failed schedule_id=%s session_id=%s",
            sch.id,
            sch.session_id,
            exc_info=True,
        )
        # Deferred-not-dropped: schedule a backoff retry (typed) instead of losing the
        # occurrence. record_fire_failure disables with a typed reason once retries run out.
        try:
            app.state.schedules.record_fire_failure(sch.id, repr(exc))
        except Exception:  # noqa: BLE001,S110 - retry bookkeeping must never kill the loop
            logger.warning(
                "scheduler tick error reason=schedule_retry_bookkeeping_failed schedule_id=%s",
                sch.id,
                exc_info=True,
            )


async def _scheduler_tick(app: "FastAPI") -> None:
    """Once-a-minute loop: fire any due schedules (local-tz cron, #21/#766/#1081).

    Each due schedule is staged through the same ``_start_background_user_turn`` engine a
    regular POST /messages uses, so the session status, ``in_flight_turns`` registration,
    cancellation, and SSE stream all behave exactly like a user turn. Errors are logged
    with a typed reason (never silently dropped), and the sleep is aligned to just past
    the next minute boundary so slow ticks don't drift past cron minutes."""

    while True:
        _scheduler_tick_once(app)
        await asyncio.sleep(_seconds_until_next_minute(datetime.now(timezone.utc)))
