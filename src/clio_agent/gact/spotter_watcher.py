"""Spotter-ai approval-mode watcher arm/disarm (server-half wiring).

``spotter-ai`` is a session ``approval_mode`` (#1034 axis) that behaves exactly
like ``ask`` at the permission gate (see
:func:`clio_agent.gact.permission_gate.default_decision`) but additionally ARMS
a dedicated watcher child — a real spawned child turn (:mod:`clio_agent.gact.turn_spawn`)
that observes the parent session's workload and may raise an ``action_card`` part
into the parent's transcript via the auto-attached ``raise_alert_card`` native tool
(:func:`clio_agent.gact.action_cards.build_raise_alert_card_tool`).

This module owns only the ARM/DISARM lifecycle:

* :func:`ensure_spotter_watcher` — idempotent arm, called after a session
  TRANSITIONS into ``spotter-ai`` mode (never on an unrelated PATCH to an
  already-armed session — see :func:`sync_watcher_for_mode`).
* :func:`disarm_spotter_watcher` — targeted cancel of the parent's live watcher
  task(s) ONLY (never the parent's other children), called after a session is
  patched AWAY FROM ``spotter-ai`` mode.

Both are wired at the route level (``gact/routes/sessions.py``) after
``sessions.create`` / ``sessions.update`` return, so the watcher lifecycle
tracks the persisted approval-mode TRANSITION, not the raw request body or the
merely-current mode.

Cold-workspace fleet race (live-integration finding): a brand-new workspace's
base-agent MCP tool fleet is built lazily on the FIRST turn that ever runs
there (see ``ClioAgent._active_tool_executor`` / ``builders.py``'s
``_dynamic_agent_tools``). Arming immediately on session create/patch can make
the watcher's own spawned turn the very first turn — racing that lazy
bring-up and failing typed (``not_implemented`` / ``custom_agent_tool_executor
_unavailable`` or ``custom_agent_tools_unavailable``). No clean
"ensure fleet ready and await" API exists today (confirmed by inspection), so
:func:`_start_fleet_retry_watchdog` is the sanctioned fallback: a bounded,
typed, backoff retry that watches the just-armed task, and on EXACTLY one of
those two typed reasons dismisses the failed row (so it never lingers in the
async tray) and re-spawns — never masking a genuinely different failure.

No silent fallback: every arm/disarm/retry/no-op path logs a typed, greppable
line (``spotter_watcher_armed`` / ``spotter_watcher_disarmed reason=...`` /
``spotter_watcher_skip reason=...`` / ``spotter_watcher_arm_failed reason=...``
/ ``spotter_watcher_arm_retry reason=fleet_cold``) through both the module
logger and :mod:`clio_agent.runtime.trace`, so a failed arm is queryable after
the fact even though it must never fail the HTTP request that triggered it
(the session still creates/patches; the watcher's absence is visible in the
tray, and the typed log is the trace).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from clio_agent.gact.agent_tasks import STATUS_FAILED
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.agent_tasks import AgentTask
    from clio_agent.gact.types import Session

logger = logging.getLogger(__name__)

#: The approval mode this module arms/disarms against.
SPOTTER_APPROVAL_MODE = "spotter-ai"

#: The display run-label stamped on the watcher's AgentTask (turn_spawn.py
#: TaskSpec.run_label), so the tray reads a named surveillance task rather
#: than an ensemble run (e.g. "spotter_watcher #1").
WATCHER_RUN_LABEL = "SPOTTER AI"

#: The requesting expert attributed for the watcher spawn. The watcher is
#: armed by the SESSION transitioning into spotter-ai mode (a route-level
#: decision), not by a declared parent expert delegating to it, so it is
#: attributed to "main" like other session-level (non-expert-declared) spawns.
_WATCHER_REQUESTING_EXPERT_ID = "main"

_DEFAULT_WATCHER_TASK_TEXT = (
    "Begin SPOTTER surveillance of your parent session's workload provenance. "
    "Follow your blueprint instructions."
)

#: The two typed ``_UnsupportedSessionAgent`` reasons (turn.py's
#: ``not_implemented`` error_info.details.reason) that mean "the workspace's
#: tool fleet was not ready yet" -- a transient race, not a real defect. ONLY
#: these two reasons trigger a retry; anything else (including a genuinely
#: unresolvable blueprint) fails through untouched.
_FLEET_COLD_REASONS = frozenset(
    {"custom_agent_tool_executor_unavailable", "custom_agent_tools_unavailable"}
)


def _watcher_blueprint_id() -> str:
    """The Agent Blueprint id the watcher child activates (config: ``spotter.watcher_blueprint_id``)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.watcher_blueprint_id",
        env="CLIO_SPOTTER_BLUEPRINT_ID",
        default="spotter-ai",
        cast=conf.as_str,
    )


def _watcher_expert_id() -> str:
    """The expert id the watcher child runs (config: ``spotter.watcher_expert_id``)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.watcher_expert_id",
        env="CLIO_SPOTTER_EXPERT_ID",
        default="spotter_watcher",
        cast=conf.as_str,
    )


def _watcher_task_text() -> str:
    """The staged task text the watcher child receives at spawn (config: ``spotter.watcher_task_text``)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.watcher_task_text",
        env="CLIO_SPOTTER_TASK_TEXT",
        default=_DEFAULT_WATCHER_TASK_TEXT,
        cast=conf.as_str,
    )


def _fleet_retry_max_attempts() -> int:
    """Bounded retry count for the cold-fleet race (config: ``spotter.fleet_retry_max_attempts``)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.fleet_retry_max_attempts",
        env="CLIO_SPOTTER_FLEET_RETRY_MAX_ATTEMPTS",
        default=6,
        cast=conf.as_int,
    )


def _fleet_retry_backoff_s() -> float:
    """Backoff between cold-fleet retries (config: ``spotter.fleet_retry_backoff_s``)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.fleet_retry_backoff_s",
        env="CLIO_SPOTTER_FLEET_RETRY_BACKOFF_S",
        default=15.0,
        cast=conf.as_float,
    )


def _fleet_retry_settle_timeout_s() -> float:
    """Per-attempt bound waiting for a spawned watcher turn to settle terminal
    before deciding "still running, leave it alone" (config:
    ``spotter.fleet_retry_settle_timeout_s``)."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.fleet_retry_settle_timeout_s",
        env="CLIO_SPOTTER_FLEET_RETRY_SETTLE_TIMEOUT_S",
        default=30.0,
        cast=conf.as_float,
    )


def _live_watcher_tasks(app: "FastAPI", parent_session_id: str) -> list["AgentTask"]:
    """Every NON-TERMINAL AgentTask for ``parent_session_id`` running the watcher expert."""

    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return []
    expert_id = _watcher_expert_id()
    return [
        task
        for task in registry.for_parent(parent_session_id)
        if not task.is_terminal and task.agent_ref.get("expert_id") == expert_id
    ]


def _child_failure_reason(app: "FastAPI", child_session_id: str) -> str:
    """The SPECIFIC typed reason from the child's final assistant message.

    ``AgentTask.error_reason`` is always the coarse ``"agent_error"`` for any
    turn-level failure (see ``turn_spawn._on_child_done``); the SPECIFIC
    ``_UnsupportedSessionAgent`` reason (e.g.
    ``custom_agent_tool_executor_unavailable``) only survives on the child's
    own final assistant message, at ``error_info.details.reason`` (turn.py's
    ``not_implemented`` mapping). Returns ``""`` when there is no such typed
    detail (message missing, no error, or a differently-shaped error).
    """

    messages = app.state.messages.get(child_session_id, []) or []
    finals = [
        m
        for m in messages
        if getattr(m, "role", "") == "assistant" and not (getattr(m, "metadata", {}) or {}).get("live")
    ]
    if not finals:
        return ""
    error_info = getattr(finals[-1], "error_info", None)
    if error_info is None:
        return ""
    details = (
        error_info.get("details")
        if isinstance(error_info, dict)
        else getattr(error_info, "details", None)
    )
    if not isinstance(details, dict):
        return ""
    return str(details.get("reason") or "")


def _spawn_watcher(app: "FastAPI", session: "Session") -> Optional["AgentTask"]:
    """One raw spawn attempt (NO idempotency check) — shared by the first arm
    in :func:`ensure_spotter_watcher` and every retry in
    :func:`_start_fleet_retry_watchdog`.

    Never raises: a spawn failure is caught, logged with a typed
    ``spotter_watcher_arm_failed`` reason, and returns ``None``.
    """

    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        SpawnError,
        TaskSpec,
        spawn_child_turn_threadsafe,
    )

    session_id = session.id
    spec = TaskSpec(
        child_expert_id=_watcher_expert_id(),
        task_text=_watcher_task_text(),
        parent_session_id=session_id,
        requesting_expert_id=_WATCHER_REQUESTING_EXPERT_ID,
        skip_declared_check=True,
        run_label=WATCHER_RUN_LABEL,
        session_scope_metadata={"active_agent_blueprint_id": _watcher_blueprint_id()},
    )
    try:
        task = spawn_child_turn_threadsafe(app, spec)
    except SpawnError as exc:
        logger.warning(
            "spotter_watcher_arm_failed reason=%s session=%s err=%r", exc.reason, session_id, exc
        )
        trace.event(
            "SPOTTER",
            "spotter_watcher_arm_failed reason=%s session=%s err=%r",
            exc.reason,
            session_id,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - arming must never fail session create/patch
        logger.warning(
            "spotter_watcher_arm_failed reason=spawn_unexpected_error session=%s err=%r",
            session_id,
            exc,
        )
        trace.event(
            "SPOTTER",
            "spotter_watcher_arm_failed reason=spawn_unexpected_error session=%s err=%r",
            session_id,
            exc,
        )
        return None

    logger.info("spotter_watcher_armed session=%s task=%s", session_id, task.task_id)
    trace.event("SPOTTER", "spotter_watcher_armed session=%s task=%s", session_id, task.task_id)
    return task


def _start_fleet_retry_watchdog(app: "FastAPI", session_id: str, task: "AgentTask") -> None:
    """Background daemon thread: watch a just-armed watcher task; on the
    cold-fleet race retry with bounded backoff, dismissing every superseded
    failed row so the async tray never accumulates more than one live watcher.

    Runs OFF the request thread — arming never blocks the triggering HTTP call
    on this. A genuinely different failure (any reason outside
    :data:`_FLEET_COLD_REASONS`) is left exactly as it failed: this never masks
    a real defect, only the specific transient race it was written for.
    """

    max_attempts = _fleet_retry_max_attempts()
    backoff_s = _fleet_retry_backoff_s()
    settle_timeout_s = _fleet_retry_settle_timeout_s()

    def _run() -> None:
        registry = app.state.agent_task_registry
        current = task
        for attempt in range(1, max_attempts + 1):
            settled = registry.event(current.task_id).wait(timeout=settle_timeout_s)
            row = registry.get(current.task_id)
            if not settled or row is None or not row.is_terminal or row.status != STATUS_FAILED:
                # Still running (or genuinely gone) after the bound: either it is
                # legitimately working (a watch loop can run indefinitely) or a
                # problem this loop is not for -- never retry-forever a runaway.
                return
            reason = _child_failure_reason(app, row.child_session_id)
            if reason not in _FLEET_COLD_REASONS:
                return  # a REAL failure -- never masked, never retried

            from clio_agent.gact.run_registry import dismiss_run  # noqa: PLC0415

            dismiss_run(app, row.task_id)
            logger.warning(
                "spotter_watcher_arm_retry reason=fleet_cold detail=%s attempt=%s/%s "
                "session=%s dismissed_task=%s",
                reason,
                attempt,
                max_attempts,
                session_id,
                row.task_id,
            )
            trace.event(
                "SPOTTER",
                "spotter_watcher_arm_retry reason=fleet_cold detail=%s attempt=%s/%s "
                "session=%s dismissed_task=%s",
                reason,
                attempt,
                max_attempts,
                session_id,
                row.task_id,
            )
            if attempt >= max_attempts:
                logger.warning(
                    "spotter_watcher_arm_failed reason=fleet_never_ready session=%s", session_id
                )
                trace.event(
                    "SPOTTER",
                    "spotter_watcher_arm_failed reason=fleet_never_ready session=%s",
                    session_id,
                )
                return

            time.sleep(backoff_s)
            live_session = app.state.sessions.get(session_id)
            if live_session is None or live_session.approval_mode != SPOTTER_APPROVAL_MODE:
                # Disarmed (or the session vanished) while we were backing off --
                # stop; re-arming would fight whatever the user asked for next.
                return
            new_task = _spawn_watcher(app, live_session)
            if new_task is None:
                return  # _spawn_watcher already logged its own typed reason
            current = new_task

    threading.Thread(
        target=_run, daemon=True, name=f"spotter-fleet-retry-{task.task_id}"
    ).start()


def ensure_spotter_watcher(app: "FastAPI", session: "Session") -> Optional["AgentTask"]:
    """Idempotently arm the spotter watcher for ``session`` when it is in spotter-ai mode.

    A no-op (returns ``None``) unless ``session.approval_mode == "spotter-ai"``. When armed,
    spawns the configured watcher expert as a real child turn
    (:func:`clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe`) bound to the watcher's
    OWN Agent Blueprint via ``session_scope_metadata`` (P2.4 #1122's spawn-binding seam,
    :func:`clio_agent.gact.spawn_context.resolve_spawn_bindings`) regardless of the parent's
    own active blueprint. ``workspace_id``/``session_mode`` are left unset so the child
    inherits them from the parent verbatim. A background watchdog then watches for the
    cold-workspace-fleet race and retries typed/bounded (see module docstring).

    Idempotent: if a NON-TERMINAL watcher task already exists for this parent, that task is
    returned unchanged and no second spawn happens.

    Never raises: a spawn failure is caught, logged with a typed ``spotter_watcher_arm_failed``
    reason, and returns ``None`` — the caller (a session create/patch route) must never fail the
    HTTP request because the watcher could not be armed.

    Args:
        app: The GACT app (agent-task registry + spawn substrate on ``app.state``).
        session: The just-created-or-patched session, already persisted with its
            resolved ``approval_mode``.

    Returns:
        The watcher's :class:`~clio_agent.gact.agent_tasks.AgentTask` (freshly spawned, or the
        already-running one), or ``None`` when not in spotter-ai mode or arming failed.
    """

    if str(getattr(session, "approval_mode", "") or "") != SPOTTER_APPROVAL_MODE:
        return None

    session_id = session.id
    existing = next(iter(_live_watcher_tasks(app, session_id)), None)
    if existing is not None:
        logger.info(
            "spotter_watcher_skip reason=already_running session=%s task=%s",
            session_id,
            existing.task_id,
        )
        trace.event(
            "SPOTTER",
            "spotter_watcher_skip reason=already_running session=%s task=%s",
            session_id,
            existing.task_id,
        )
        return existing

    task = _spawn_watcher(app, session)
    if task is not None:
        _start_fleet_retry_watchdog(app, session_id, task)
    return task


def sync_watcher_for_mode(
    app: "FastAPI", session: "Session", *, prior_approval_mode: str = ""
) -> None:
    """Route hook: arm/disarm the watcher off ``session``'s PERSISTED TRANSITION.

    The single call site both ``create_session`` and ``patch_session``
    (``gact/routes/sessions.py``) use so neither route has to inline the
    arm-vs-disarm branch itself.

    Arms ONLY on a genuine transition INTO spotter-ai (``prior_approval_mode``
    differs from the current mode — a fresh create has no prior mode, so it
    always qualifies). Disarms ONLY on a genuine transition AWAY from it. An
    unrelated PATCH (mode unchanged, spotter-ai before and after — e.g. a
    title rename, a pin toggle) is a true no-op: neither branch runs.

    This distinction matters beyond idempotency: once a watcher's own turn
    settles (completed/failed), a session sitting in spotter-ai still reads
    ``approval_mode == "spotter-ai"`` on every LATER unrelated PATCH. Arming
    on CURRENT mode alone would re-spawn a FRESH watcher on every such PATCH —
    which would re-detect the SAME already-reported anomaly and double-fire
    the alert while the user is mid-``Discuss`` on the first one. Arming only
    on the actual transition means re-arming after a watcher's own turn ends
    requires an explicit mode round-trip (PATCH away, then back), never a
    side effect of an unrelated field edit.

    Args:
        app: The GACT app.
        session: The just-created-or-patched session, already persisted.
        prior_approval_mode: The session's approval_mode BEFORE this call (empty
            for a fresh create, where there is no prior mode).
    """

    entered_spotter_ai = (
        session.approval_mode == SPOTTER_APPROVAL_MODE
        and prior_approval_mode != SPOTTER_APPROVAL_MODE
    )
    left_spotter_ai = (
        prior_approval_mode == SPOTTER_APPROVAL_MODE
        and session.approval_mode != SPOTTER_APPROVAL_MODE
    )
    if entered_spotter_ai:
        ensure_spotter_watcher(app, session)
    elif left_spotter_ai:
        disarm_spotter_watcher(app, session)


def disarm_spotter_watcher(
    app: "FastAPI", session: "Session", *, reason: str = "mode_changed"
) -> int:
    """Cancel the parent's live watcher task(s) ONLY — never its other children.

    Uses the targeted per-task cancel primitive
    (:func:`clio_agent.gact.turn_spawn.cancel_agent_task`), NOT
    :func:`clio_agent.gact.turn_spawn.cancel_children_of` (which cascades to every
    descendant of the parent, spotter-armed or not).

    Args:
        app: The GACT app (agent-task registry + cancel substrate on ``app.state``).
        session: The session that just left spotter-ai mode (its NEW approval_mode,
            already persisted).
        reason: The typed reason recorded on the disarm log line. Defaults to
            ``"mode_changed"`` (the only caller today: a PATCH that flips
            ``approval_mode`` away from ``"spotter-ai"``).

    Returns:
        The count of watcher tasks actually cancelled (0 when none were live —
        logged as a typed no-op, not silently ignored).
    """

    session_id = session.id
    live = _live_watcher_tasks(app, session_id)
    if not live:
        logger.info("spotter_watcher_skip reason=no_active_watcher session=%s", session_id)
        trace.event(
            "SPOTTER", "spotter_watcher_skip reason=no_active_watcher session=%s", session_id
        )
        return 0

    from clio_agent.gact.turn_spawn import cancel_agent_task  # noqa: PLC0415

    cancelled = sum(1 for task in live if cancel_agent_task(app, task.task_id))
    logger.info(
        "spotter_watcher_disarmed reason=%s session=%s count=%s", reason, session_id, cancelled
    )
    trace.event(
        "SPOTTER",
        "spotter_watcher_disarmed reason=%s session=%s count=%s",
        reason,
        session_id,
        cancelled,
    )
    return cancelled
