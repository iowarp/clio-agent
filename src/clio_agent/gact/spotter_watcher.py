"""Spotter-ai approval-mode watcher arm/disarm (server-half wiring).

``spotter-ai`` is a session ``approval_mode`` (#1034 axis) that behaves exactly
like ``ask`` at the permission gate (see
:func:`clio_agent.gact.permission_gate.default_decision`) but additionally ARMS
a dedicated watcher child — a real spawned child turn (:mod:`clio_agent.gact.turn_spawn`)
that observes the parent session's workload and may raise an ``action_card`` part
into the parent's transcript via the auto-attached ``raise_alert_card`` native tool
(:func:`clio_agent.gact.action_cards.build_raise_alert_card_tool`).

This module owns only the ARM/DISARM lifecycle:

* :func:`ensure_spotter_watcher` — idempotent arm, called after a session is
  created or patched INTO ``spotter-ai`` mode.
* :func:`disarm_spotter_watcher` — targeted cancel of the parent's live watcher
  task(s) ONLY (never the parent's other children), called after a session is
  patched AWAY FROM ``spotter-ai`` mode.

Both are wired at the route level (``gact/routes/sessions.py``) after
``sessions.create`` / ``sessions.update`` return, so the watcher lifecycle
tracks the persisted approval-mode transition, not the raw request body.

No silent fallback: every arm/disarm/no-op path logs a typed, greppable line
(``spotter_watcher_armed`` / ``spotter_watcher_disarmed reason=...`` /
``spotter_watcher_skip reason=...`` / ``spotter_watcher_arm_failed reason=...``)
through both the module logger and :mod:`clio_agent.runtime.trace`, so a failed
arm is queryable after the fact even though it must never fail the HTTP request
that triggered it (the session still creates/patches; the watcher's absence is
visible in the tray, and the typed log is the trace).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

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


def ensure_spotter_watcher(app: "FastAPI", session: "Session") -> Optional["AgentTask"]:
    """Idempotently arm the spotter watcher for ``session`` when it is in spotter-ai mode.

    A no-op (returns ``None``) unless ``session.approval_mode == "spotter-ai"``. When armed,
    spawns the configured watcher expert as a real child turn
    (:func:`clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe`) bound to the watcher's
    OWN Agent Blueprint via ``session_scope_metadata`` (P2.4 #1122's spawn-binding seam,
    :func:`clio_agent.gact.spawn_context.resolve_spawn_bindings`) regardless of the parent's
    own active blueprint. ``workspace_id``/``session_mode`` are left unset so the child
    inherits them from the parent verbatim.

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

    from clio_agent.gact.turn_spawn import (  # noqa: PLC0415
        SpawnError,
        TaskSpec,
        spawn_child_turn_threadsafe,
    )

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
            "spotter_watcher_arm_failed reason=%s session=%s err=%r",
            exc.reason,
            session_id,
            exc,
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


def sync_watcher_for_mode(
    app: "FastAPI", session: "Session", *, prior_approval_mode: str = ""
) -> None:
    """Route hook: arm/disarm the watcher off ``session``'s PERSISTED transition.

    The single call site both ``create_session`` and ``patch_session``
    (``gact/routes/sessions.py``) use so neither route has to inline the
    arm-vs-disarm branch itself. Arms (idempotently) when the CURRENT mode is
    spotter-ai; disarms ONLY when ``prior_approval_mode`` was spotter-ai and the
    current mode is not — a create call (no prior mode to pass) therefore never
    disarms. An unrelated PATCH (never spotter-ai before or after) is a true
    no-op: neither branch runs, so it never touches the agent-task registry.

    Args:
        app: The GACT app.
        session: The just-created-or-patched session, already persisted.
        prior_approval_mode: The session's approval_mode BEFORE this call (empty
            for a fresh create, where there is no prior mode).
    """

    if session.approval_mode == SPOTTER_APPROVAL_MODE:
        ensure_spotter_watcher(app, session)
    elif prior_approval_mode == SPOTTER_APPROVAL_MODE:
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
