"""Scheduler tick + fire runtime for the GACT server (#21, #766, #1081).

Owner module for the background scheduler loop, extracted from ``gact/app.py`` (the
no-accretion ground rule: app.py is at its ratchet baseline). It hosts the four
helpers the boot-time ``_lifespan`` and the scheduler tests use:

* :func:`_scheduler_tick` — the once-a-minute loop, boundary-aligned to avoid drift;
* :func:`_scheduler_tick_once` — process one tick: retry deferred, then fire due;
* :func:`_fire_schedule` — confirm one due occurrence in its Core session mailbox;
* :func:`_seconds_until_next_minute` — the boundary-aligned inter-tick sleep.

``app.py`` re-exports all four for compatibility. Occurrences use the same
Core mailbox and TurnRunner admission as feedback and task results, including
next-iteration delivery when the session is busy.

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

from clio_agent.gact.autonomy_guard import (
    admit_loop_wakeup,
    prepare_scheduled_host,
    reconcile_autonomous_controls,
    settle_failed_loop_schedule,
)
from clio_agent.gact.session_input import accept_session_input

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

    if not admit_loop_wakeup(app, sch):
        return
    sess = app.state.sessions.get(sch.session_id)
    if sess is None:
        app.state.schedules.mark_fired(sch.id)
        logger.warning(
            "scheduler tick error reason=schedule_session_not_found schedule_id=%s session_id=%s",
            sch.id,
            sch.session_id,
        )
        return
    if (hasattr(app.state, "agent") and app.state.agent is None) or (
        getattr(app.state, "lm_config_status", {}).get("state") == "configuring"
    ):
        app.state.deferred_schedules.add(sch.id)
        logger.info(
            "scheduler tick deferred reason=schedule_agent_unavailable schedule_id=%s session_id=%s",
            sch.id,
            sch.session_id,
        )
        return
    busy = app.state.turn_runner.busy(sch.session_id)
    if busy and str(getattr(sch, "overlap_policy", "queue") or "queue") == "skip":
        app.state.deferred_schedules.discard(sch.id)
        app.state.schedules.mark_fired(sch.id)
        logger.info(
            "scheduler overlap skipped schedule_id=%s session_id=%s", sch.id, sch.session_id
        )
        return
    from clio_agent.gact.autonomous_loop import _get_loop

    loop = _get_loop(app, sch.session_id)
    loop_id = loop.get("loop_id") if loop.get("pending_schedule_id") == sch.id else None
    accept_session_input(
        app,
        sch.session_id,
        sch.question,
        metadata={
            "scheduled": True,
            "schedule_id": sch.id,
            "schedule_one_shot": not sch.recurring,
            **({"loop_id": loop_id} if loop_id else {}),
        },
        steer_message_id=f"schedule:{sch.id}:{sch.fire_count}",
    )
    # Confirmation precedes occurrence advancement. A retry of a failed advancement
    # uses the same Core message identity and never admits a duplicate occurrence.
    app.state.schedules.mark_fired(sch.id)
    app.state.deferred_schedules.discard(sch.id)


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
    try:
        from clio_agent.gact.memory.background import memory_tick  # noqa: PLC0415

        memory_tick(app, now)  # long-term memory's background pass (idle sessions)
    except Exception:  # noqa: BLE001 - the tick loop must survive the memory pass
        logger.warning("scheduler tick error reason=memory_pass_failed", exc_info=True)


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
            settle_failed_loop_schedule(app, sch)
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

    reconciled = False
    while True:
        try:
            if not reconciled:
                await asyncio.to_thread(reconcile_autonomous_controls, app)
                reconciled = True
            await prepare_scheduled_host(app)
            from clio_agent.gact.goal_control import maintain_goal_controls

            await maintain_goal_controls(app)
        except (OSError, RuntimeError):
            logger.error("scheduler tick delayed reason=control_preparation_failed", exc_info=True)
        else:
            await asyncio.to_thread(_scheduler_tick_once, app)
        await asyncio.sleep(_seconds_until_next_minute(datetime.now(timezone.utc)))
