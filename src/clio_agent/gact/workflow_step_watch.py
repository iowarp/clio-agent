"""Progress-based step liveness watch for declared workflows (#992).

Owner module for the per-step liveness half of the declared-workflow runner
(:mod:`clio_agent.gact.workflows`), carved out so that file stays focused on
declaration + parsing + the runner (no-accretion, #775). The runner calls
:func:`watch_step` after spawning each step's child and acts on the returned verdict.

**The principle (the owner's liveness rule).** A legitimately-heavy step — e.g. one
fusing three live feature services — may run far longer than any fixed budget while
making steady progress; a wall-clock bound misclassifies it as stalled (the #948
final-gate finding, sess_66643f9600a4). So a step is stalled when its child shows NO
observable ACTIVITY for the inactivity window, NOT when its total duration exceeds a
bound. A pack may still declare an explicit absolute budget per step (``step.timeout_s``)
as an opt-in hard wall; absent one, only inactivity can stop a progressing child.

**The activity signal.** :func:`step_activity_monotonic` reads the child session's bus
heartbeat (:meth:`clio_agent.gact.events.EventBus.last_publish_monotonic` — every message
delta, tool part, semantic span, and ``agent.task.*`` lifecycle event the child turn makes
flows through it), plus an in-flight LM call owned by the child (a deep-reasoning model
streaming chain-of-thought on a separate channel, run synchronously in an executor with no
live deltas) — the same two signals the per-turn no-progress watchdog trusts
(:func:`clio_agent.gact.turn_watchdog.await_turn_work`). It is the single activity PROBE
seam: neutralize it and a slow-but-active child looks inactive and wrongly stalls — the
#992 sabotage lock.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from clio_agent import conf
from clio_agent.runtime.lm_activity import lm_call_in_flight

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.agent_tasks import AgentTask

# Config-first per #985: file ``workflows.step_inactivity_s`` → env
# ``CLIO_WORKFLOW_STEP_INACTIVITY_S`` → default. The no-activity window in seconds.
_DEFAULT_STEP_INACTIVITY_S = 120.0


def resolve_step_inactivity_s() -> float:
    """The configured no-activity window in seconds (file → env → default, #985/#992).

    Read once per :func:`clio_agent.gact.workflows.run_declared_workflow` call when the
    caller does not pass an explicit ``inactivity_window_s``. A non-positive / malformed
    value falls back to the default (never an accidental 0 that would stall every step).
    """

    try:
        value = conf.resolve(
            "workflows.step_inactivity_s",
            env="CLIO_WORKFLOW_STEP_INACTIVITY_S",
            default=_DEFAULT_STEP_INACTIVITY_S,
            cast=conf.as_float,
        )
    except (ValueError, TypeError):
        return _DEFAULT_STEP_INACTIVITY_S
    return value if value > 0 else _DEFAULT_STEP_INACTIVITY_S


def step_activity_monotonic(app: "FastAPI", child_session_id: str, *, now: float) -> float:
    """The most-recent observable-progress ``time.monotonic`` for a child session (#992).

    The child's bus publishes are the PRIMARY signal (see module docstring). An in-flight
    LM call OWNED by the child also counts as live progress even when it publishes nothing.
    Returns ``now`` when the child's LM call is generating, else the child's last bus
    heartbeat (``0.0`` if the child has published nothing yet).
    """

    if lm_call_in_flight(child_session_id):
        return now
    return app.state.bus.last_publish_monotonic(child_session_id)


def step_watch_poll_s(inactivity_window_s: float, absolute_budget_s: float) -> float:
    """Poll cadence for the progress watch: short enough to bound abort latency after a
    child truly goes silent, capped by the smallest active window so a tiny configured
    budget still polls at least as often (never busy-waits below 20ms)."""

    windows = [w for w in (inactivity_window_s, absolute_budget_s) if w and w > 0]
    return max(0.02, min([2.0, *windows]))


def watch_step(
    app: "FastAPI",
    task_id: str,
    child_session_id: str,
    *,
    inactivity_window_s: float,
    absolute_budget_s: float,
) -> tuple[Optional["AgentTask"], str, float]:
    """Watch a step's child to a terminal state OR a liveness verdict (#992).

    Returns ``(task, outcome, observed_inactivity_s)`` where ``outcome`` is one of:

    * ``"terminal"`` — the child reached a terminal status (its wait-Event fired). The
      caller distinguishes completed vs failed off the returned record.
    * ``"stalled"`` — the child showed NO observable activity for ``inactivity_window_s``
      while still non-terminal (the progress-based liveness stall). ``observed_inactivity_s``
      carries the measured silent gap.
    * ``"timeout"`` — the child exceeded the pack-DECLARED ``absolute_budget_s`` while still
      non-terminal (the opt-in absolute budget). Only reachable when a step declares one.
    * ``"unknown"`` — the task is not on the registry (never spawned / already gone).

    The absolute budget (when declared) is checked FIRST each tick, so a legitimately-active
    child that a pack chose to hard-bound stops at its budget rather than running forever;
    absent a budget, only inactivity can stop a progressing child.
    """

    reg = app.state.agent_task_registry
    if reg.get(task_id) is None:
        return None, "unknown", 0.0
    event = reg.event(task_id)
    started = time.monotonic()
    last_activity = started
    poll = step_watch_poll_s(inactivity_window_s, absolute_budget_s)
    while True:
        if event.wait(timeout=poll):
            return reg.get(task_id), "terminal", 0.0
        now = time.monotonic()
        if absolute_budget_s and absolute_budget_s > 0 and now - started >= absolute_budget_s:
            return reg.get(task_id), "timeout", now - last_activity
        activity = step_activity_monotonic(app, child_session_id, now=now)
        if activity > last_activity:
            last_activity = activity
        if inactivity_window_s > 0 and now - last_activity >= inactivity_window_s:
            return reg.get(task_id), "stalled", now - last_activity
