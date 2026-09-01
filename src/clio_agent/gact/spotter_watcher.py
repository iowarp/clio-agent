"""Spotter-ai approval-mode standing watcher: arm/disarm + push-wake (server-half).

``spotter-ai`` is a session ``approval_mode`` (#1034 axis) that behaves exactly
like ``ask`` at the permission gate (see
:func:`clio_agent.gact.permission_gate.default_decision`) but additionally ARMS
a dedicated watcher child session that observes the parent session's workload
and may raise an ``action_card`` part into the parent's transcript via the
auto-attached ``raise_alert_card`` native tool
(:func:`clio_agent.gact.action_cards.build_raise_alert_card_tool`).

Owner design ruling (verbatim intent): "the agent should wait, until an input;
the agent session should be there sitting, awaiting data, and data should be
pushed into it." **No timers anywhere.** This module implements the STANDING
PUSH-WAKE shape:

* :func:`ensure_spotter_watcher` — idempotent ARM: mints the watcher's child
  session + a STANDING :class:`~clio_agent.gact.agent_tasks.AgentTask` via
  ``turn_spawn.TaskSpec(start_turn=False)`` — the record transitions straight
  to ``status=STATUS_RUNNING`` (never terminal while armed; DISARM is the only
  path out) with ``live_state="waiting"``, but **no turn is ever started at
  arm time**. Called after a session TRANSITIONS into ``spotter-ai`` mode
  (never on an unrelated PATCH to an already-armed session — see
  :func:`sync_watcher_for_mode`).
* :func:`wake_on_parent_activity` / :func:`on_turn_finalized` — real parent
  activity pushes a factual wake through the loop inbox. An idle watcher starts
  a check turn immediately; a busy watcher retains at most one coalesced wake.
  Finalizing the watcher's own check returns its standing task to ``waiting``.
  The first check always follows real parent activity, after workspace tools
  have warmed, so no timer-based cold-start workaround is needed.
* :func:`disarm_spotter_watcher` — targeted cancel of the parent's live watcher
  task(s) ONLY (never the parent's other children) via the existing targeted
  per-task cancel primitive (:func:`clio_agent.gact.turn_spawn.cancel_agent_task`),
  which is ALREADY correct for a standing task unchanged: it transitions
  ``status`` RUNNING -> CANCELLED (terminal) and cooperatively/hard-cancels any
  in-flight check turn (a no-op when the watcher is idle/"waiting", since there
  is nothing in flight to cancel). Called after a session is PATCHED AWAY FROM
  ``spotter-ai`` mode.

``sync_watcher_for_mode`` is wired at the route level (``gact/routes/sessions.py``)
after ``sessions.create`` / ``sessions.update`` return. ``wake_on_parent_activity``
is wired at the tool-observer's ``tool.call.completed`` seam
(``gact/tool_observer.py``); ``on_turn_finalized`` is wired at the turn-finalize
seam (``gact/turn_finalize.py``'s ``finalize_turn``, right after its
``session.status_changed`` publish, alongside the existing P2.3/P4.x
``dispatch_*_at_finalize`` hooks it mirrors).

No silent fallback: every arm/disarm/wake/coalesce/no-op path logs a typed,
greppable line (``spotter_watcher_armed`` / ``spotter_watcher_disarmed
reason=...`` / ``spotter_watcher_skip reason=...`` / ``spotter_watcher_arm_failed
reason=...`` / ``spotter_wake_started`` / ``spotter_wake_enqueued`` /
``spotter_wake_coalesced reason=...``) through both the module logger and
:mod:`clio_agent.runtime.trace`.

The PROTECTED-PARENT half of the barrier — the clearance event this module
signals, the typed fail-closed reasons, and the per-exchange progress wait a
mutating tool call performs — lives in its own owner module,
:mod:`clio_agent.gact.spotter_clearance`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.agent_tasks import STATUS_RUNNING
from clio_agent.gact.spotter_clearance import release_clearance_event, signal_clearance
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

#: The loop-inbox coalesce key every spotter wake shares, so a burst of parent
#: tool completions during one running check turn collapses onto AT MOST ONE
#: buffered wake (see ``loop_inbox.LoopInbox.put_coalesced_user_message``).
_WAKE_COALESCE_KEY = "spotter_wake"

#: Turn-final wake payload (#1218 e): the parent session's OWN turn just ended
#: (the ``turn.completed`` half of the push, vs. a specific tool completing).
#: FACTS ONLY — no directive ("check the store...", "act per your
#: instructions..."); the watcher's OWN prompt owns what to do about it.
_WAKE_TURN_FINAL_TEXT = (
    "Watched session activity: the session's turn completed. "
    "The provenance store may have new entries."
)

#: Coalesced-burst wake payload (#1218 e): several parent activities collapsed
#: onto the ONE buffered wake behind an already-running check turn (never more
#: than one queued — see :func:`_push_wake`). A deliberately generic summary
#: since no single tool/outcome is representative of the whole burst.
_WAKE_COALESCED_TEXT = (
    "Watched session activity: multiple tool calls completed. "
    "The provenance store may have new entries."
)


def _structural_item_identity(value: Any) -> str:
    """Return a bounded identity copied from one typed result item.

    The wake lane must distinguish successive equal-sized batches without
    authoring a semantic summary. Prefer explicit identifier fields on
    mapping-shaped items; scalar list entries are already their own identity.
    """

    if isinstance(value, Mapping):
        for key in ("run_id", "task_id", "artifact_id", "verdict_id", "id"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate[:80]
        return ""
    if isinstance(value, str):
        return str(value).strip()[:80]
    return ""


def _tool_outcome_fact(structured_result: Any) -> str:
    """A short, STRUCTURAL fact drawn from a completed tool's own typed result
    (#1218 e) — never an authored/inferred summary. When the result is a
    mapping, the first list-valued entry's length is reported using that
    entry's OWN key as the noun (e.g. ``{"verdicts": [...]*5}`` -> ``"5
    verdicts reported"``), so the fact is always traceable to real wire data.
    When the items expose structural identifiers, the first/last identifiers
    are included. This keeps equal-sized successive batches causally distinct
    without re-narrating or inferring anything from their contents.
    Returns ``""`` when the result carries no list-shaped evidence (a tool
    that returns scalars/None yields no fabricated count — never a guess).
    """

    if not isinstance(structured_result, Mapping):
        return ""
    for key, value in structured_result.items():
        if isinstance(value, (list, tuple)):
            noun = str(key).replace("_", " ").strip() or "items"
            fact = f"{len(value)} {noun} reported"
            identities = [
                identity for item in value if (identity := _structural_item_identity(item))
            ]
            if identities:
                if len(identities) == 1 or identities[0] == identities[-1]:
                    return f"{fact}: {identities[0]}"
                return f"{fact}: {identities[0]} through {identities[-1]}"
            return fact
    return ""


def on_turn_runner_idle(app: "FastAPI", session_id: str) -> None:
    """Wake a clearance waiter after the watcher slot is actually released."""

    session = app.state.sessions.get(session_id)
    parent_id = str(getattr(session, "parent_session_id", "") or "") if session else ""
    if not parent_id:
        return
    task = next(
        (
            item
            for item in _live_watcher_tasks(app, parent_id)
            if item.child_session_id == session_id
        ),
        None,
    )
    if task is not None:
        signal_clearance(app, parent_id)


def _wake_text(*, tool_name: str = "", tool_ok: bool = True, tool_result: Any = None) -> str:
    """Compose the fact-carrying wake payload for ONE (non-coalesced) push
    (#1218 e). ``tool_name`` empty means the PARENT's own turn just finished
    (:func:`on_turn_finalized`'s branch) — otherwise it names the tool that
    just completed on the protected parent session, plus (when derivable) a
    structural fact about what it reported. FACTS ONLY: no directive verbs —
    the watcher's own prompt (not this payload) owns its reasoning about what
    to do next.
    """

    if not tool_name:
        return _WAKE_TURN_FINAL_TEXT
    if not tool_ok:
        return (
            f"Watched session activity: {tool_name} failed. "
            "The provenance store may have new entries."
        )
    fact = _tool_outcome_fact(tool_result)
    suffix = f" ({fact})" if fact else ""
    return (
        f"Watched session activity: {tool_name} completed{suffix}. "
        "The provenance store may have new entries."
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


def _live_watcher_tasks(app: "FastAPI", parent_session_id: str) -> list["AgentTask"]:
    """Every NON-TERMINAL AgentTask for ``parent_session_id`` running the watcher expert.

    A standing, armed watcher's ``status`` stays ``STATUS_RUNNING`` for its
    WHOLE armed lifetime (never terminal until disarm — see the module
    docstring), so this is also the "is a watcher currently armed" check.
    """

    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return []
    expert_id = _watcher_expert_id()
    return [
        task
        for task in registry.for_parent(parent_session_id)
        if not task.is_terminal and task.agent_ref.get("expert_id") == expert_id
    ]


def _set_live_state(
    app: "FastAPI", task_id: str, live_state: str, *, error_reason: str = ""
) -> Optional["AgentTask"]:
    """Flip a standing watcher task's ``live_state`` WITHOUT a status transition.

    ``AgentTaskRegistry.transition()`` always forces ``live_state == new_status``
    (see its ``updates`` dict) — it cannot produce a RUNNING task with
    ``live_state="waiting"``. This uses the registry's plain ``register()`` (a
    pure index-replace, no legality checks) via ``dataclasses.replace`` instead,
    which ``AgentTask.__post_init__`` allows to survive: the reset-to-status
    branch only fires when ``status != STATUS_RUNNING`` or ``live_state`` is
    falsy/itself a status name, neither of which applies to ``"waiting"``.

    A no-op (returns ``None``, typed-logged) if the task is gone or (a safety
    guard against a race with disarm) no longer ``STATUS_RUNNING`` — a
    ``live_state`` adjustment only makes sense for an armed/running standing
    record, never a queued/terminal one.
    """

    from clio_agent.gact.agent_tasks import ERROR_REASONS, persist_agent_task  # noqa: PLC0415

    registry = app.state.agent_task_registry
    current = registry.get(task_id)
    if current is None or current.status != STATUS_RUNNING:
        logger.info(
            "spotter_watcher_live_state_skip reason=not_running task=%s live_state=%s",
            task_id,
            live_state,
        )
        return None
    normalized_reason = error_reason if error_reason in ERROR_REASONS else ""
    if live_state == "error" and not normalized_reason:
        normalized_reason = "agent_error"
    if current.live_state == live_state and current.error_reason == normalized_reason:
        return current  # already there -- idempotent, no redundant persist/publish
    updated = replace(current, live_state=live_state, error_reason=normalized_reason)
    registry.register(updated)
    persist_agent_task(app, updated)
    return updated


def _arm_watcher(app: "FastAPI", session: "Session") -> Optional["AgentTask"]:
    """One raw ARM attempt (NO idempotency check) — mints the watcher child
    session + a STANDING AgentTask, WITHOUT starting a turn.

    Never raises: a mint failure is caught, logged with a typed
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
        task_text="",  # unused: start_turn=False never launches a turn
        parent_session_id=session_id,
        requesting_expert_id=_WATCHER_REQUESTING_EXPERT_ID,
        skip_declared_check=True,
        run_label=WATCHER_RUN_LABEL,
        session_scope_metadata={"active_agent_blueprint_id": _watcher_blueprint_id()},
        session_approval_profile="spotter-watcher",
        start_turn=False,
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

    # Standing default (module docstring): armed and idle, awaiting the first push.
    task = _set_live_state(app, task.task_id, "waiting") or task
    logger.info("spotter_watcher_armed session=%s task=%s", session_id, task.task_id)
    trace.event("SPOTTER", "spotter_watcher_armed session=%s task=%s", session_id, task.task_id)
    return task


def ensure_spotter_watcher(app: "FastAPI", session: "Session") -> Optional["AgentTask"]:
    """Idempotently ARM the standing spotter watcher for ``session``.

    A no-op (returns ``None``) unless ``session.approval_mode == "spotter-ai"``.
    When armed, mints the watcher child session + a STANDING AgentTask (see
    :func:`_arm_watcher` / the module docstring) bound to the watcher's OWN
    Agent Blueprint via ``session_scope_metadata`` (P2.4 #1122's spawn-binding
    seam, :func:`clio_agent.gact.spawn_context.resolve_spawn_bindings`)
    regardless of the parent's own active blueprint.

    Idempotent: if a NON-TERMINAL (i.e. still-armed) watcher task already
    exists for this parent, that task is returned unchanged and no second arm
    happens.

    Never raises: a mint failure is caught, logged with a typed
    ``spotter_watcher_arm_failed`` reason, and returns ``None`` — the caller (a
    session create/patch route) must never fail the HTTP request because the
    watcher could not be armed.

    Args:
        app: The GACT app (agent-task registry + spawn substrate on ``app.state``).
        session: The just-created-or-patched session, already persisted with its
            resolved ``approval_mode``.

    Returns:
        The watcher's :class:`~clio_agent.gact.agent_tasks.AgentTask` (freshly
        armed, or the already-armed one), or ``None`` when not in spotter-ai
        mode or arming failed.
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

    return _arm_watcher(app, session)


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
    descendant of the parent, spotter-armed or not). Already correct for the
    standing-task shape unchanged: it transitions ``status`` RUNNING ->
    CANCELLED (a legal transition, terminal) and cooperatively/hard-cancels any
    in-flight check turn — a no-op when the watcher is idle/"waiting", since
    there is nothing in flight to cancel.

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
    # Bounded retention: the parent's clearance event outlives nothing — release
    # it here (waiters wake and fail closed typed, since no watcher is live).
    release_clearance_event(app, session_id)
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


# --------------------------------------------------------------------------- #
# Push-wake: real PARENT activity wakes the armed watcher. No timers.
# --------------------------------------------------------------------------- #


def wake_on_parent_activity(
    app: "FastAPI",
    parent_session_id: str,
    *,
    tool_name: str = "",
    ok: bool = True,
    result: Any = None,
) -> None:
    """Push-wake hook: PARENT session activity wakes its armed watcher.

    Owner design: "mode is standing intent; data should be PUSHED into it."
    Wired at the tool-observer's ``tool.call.completed`` seam
    (``gact/tool_observer.py``) so every completed tool call on a spotter-ai
    session's own turn(s) is a wake trigger — this is the ``tool.call.completed``
    half of the push; :func:`on_turn_finalized` covers ``turn.completed``.

    A cheap no-op unless ``parent_session_id`` is genuinely in spotter-ai mode
    with a LIVE (armed) watcher — the common case for every OTHER session's
    tool calls in the whole system.

    Args:
        app: The GACT app.
        parent_session_id: The protected (spotter-ai) session whose activity
            is pushing this wake.
        tool_name: The tool that just completed on ``parent_session_id`` — the
            ``tool.call.completed`` caller's fact (#1218 e). Empty (the
            default) means this is a ``turn.completed`` push instead
            (:func:`on_turn_finalized`'s own-branch call), which carries no
            single tool to name.
        ok: Whether ``tool_name`` completed successfully. Ignored when
            ``tool_name`` is empty.
        result: The tool's own typed result (its ``structuredContent`` when
            available) — read STRUCTURALLY for a short fact (see
            :func:`_tool_outcome_fact`), never re-narrated. Ignored when
            ``tool_name`` is empty.
    """

    session = app.state.sessions.get(parent_session_id)
    if session is None or session.approval_mode != SPOTTER_APPROVAL_MODE:
        return
    task = next(iter(_live_watcher_tasks(app, parent_session_id)), None)
    if task is None:
        return
    wake_text = _wake_text(tool_name=tool_name, tool_ok=ok, tool_result=result)
    _push_wake(app, parent_session_id, task, wake_text)


def _start_check_turn_on_app_loop(
    app: "FastAPI", child_sid: str, child_session: "Session", wake_text: str
) -> None:
    """Start the watcher's check turn, marshaled onto the app's event loop
    regardless of the calling thread (mirrors
    :func:`clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe`'s exact
    pattern). ``TurnRunner.spawn`` (``_start_background_user_turn``'s tail)
    calls ``loop.create_task(...)`` directly, which is only safe FROM the loop
    thread itself — and this hook's callers (the tool-observer's
    ``tool.call.completed`` seam in particular) are not guaranteed to already
    be on it (a tool call can execute on a worker thread)."""

    import asyncio  # noqa: PLC0415

    from clio_agent.gact.turn import _start_background_user_turn  # noqa: PLC0415

    def _run() -> None:
        _start_background_user_turn(app, child_sid, child_session, wake_text, prev_status="idle")

    loop = getattr(app.state, "mcp_app_loop", None)
    if loop is None:
        _run()
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _run()
        return

    async def _call() -> None:
        _run()

    asyncio.run_coroutine_threadsafe(_call(), loop).result(timeout=30)


def _push_wake(app: "FastAPI", parent_session_id: str, task: "AgentTask", wake_text: str) -> None:
    """Push one wake into the watcher's session: start a check turn if idle,
    coalesce onto an already-pending one if busy — never more than one
    buffered wake behind a running check turn (a 20-tool-call parent turn
    must produce at most ONE queued wake, not 20).

    A burst that coalesces does not just drop the newer facts on the floor
    (#1218 e): the SECOND-and-later push in a busy window overwrites the
    buffered wake with the generic multi-activity summary
    (:data:`_WAKE_COALESCED_TEXT`) via the coalesce key's replace semantics
    (:meth:`clio_agent.gact.loop_inbox.LoopInbox.put_coalesced_user_message`)
    — still exactly one buffered wake, now honestly describing "several
    things happened" instead of stale single-event text.
    """

    child_sid = task.child_session_id
    runner = getattr(app.state, "turn_runner", None)
    if runner is None:
        return

    if runner.busy(child_sid):
        from clio_agent.gact.loop_inbox import (  # noqa: PLC0415
            enqueue_user_steer,
            inbox_for,
        )

        coalescing = inbox_for(app, child_sid).peek_nonempty()
        text = _WAKE_COALESCED_TEXT if coalescing else wake_text
        enqueue_user_steer(app, child_sid, text, {"coalesce_key": _WAKE_COALESCE_KEY})
        if coalescing:
            logger.info(
                "spotter_wake_coalesced reason=already_pending session=%s watcher=%s",
                parent_session_id,
                child_sid,
            )
            trace.event(
                "SPOTTER",
                "spotter_wake_coalesced reason=already_pending session=%s watcher=%s",
                parent_session_id,
                child_sid,
            )
        else:
            logger.info(
                "spotter_wake_enqueued session=%s watcher=%s task=%s",
                parent_session_id,
                child_sid,
                task.task_id,
            )
            trace.event(
                "SPOTTER",
                "spotter_wake_enqueued session=%s watcher=%s task=%s",
                parent_session_id,
                child_sid,
                task.task_id,
            )
        signal_clearance(app, parent_session_id)
        return

    child_session = app.state.sessions.get(child_sid)
    if child_session is None:
        return

    _start_check_turn_on_app_loop(app, child_sid, child_session, wake_text)
    _set_live_state(app, task.task_id, "running")
    signal_clearance(app, parent_session_id)
    logger.info(
        "spotter_wake_started session=%s watcher=%s task=%s",
        parent_session_id,
        child_sid,
        task.task_id,
    )
    trace.event(
        "SPOTTER",
        "spotter_wake_started session=%s watcher=%s task=%s",
        parent_session_id,
        child_sid,
        task.task_id,
    )


def on_turn_finalized(app: "FastAPI", session_id: str) -> None:
    """Turn-finalize hook: fires for EVERY session's turn ending — cheap no-op
    checks when neither case applies (the overwhelming common case).

    Two DISTINCT things a finalizing turn can mean here:

    * ``session_id`` is a spotter-ai PARENT with a live watcher: its turn
      finishing is parent activity too (the ``turn.completed`` half of the
      push; :func:`wake_on_parent_activity` covers ``tool.call.completed``) —
      push a wake.
    * ``session_id`` IS a watcher's OWN child session (a live standing task
      whose ``child_session_id`` matches): its CHECK turn (or a direct
      "Discuss" user turn — both are ordinary turns on this session) just
      ended — inspect its authoritative final assistant message. A successful
      check returns to ``"waiting"``; a typed failure remains armed but enters
      ``live_state="error"`` and blocks protected mutations until a later
      successful recovery check. Never a ``status`` transition; disarm stays
      the only path to terminal.

    A session is never both (the watcher's own session keeps the public
    ``approval_mode="ask"`` and carries only the server-owned narrow
    ``approval_profile="spotter-watcher"`` — it is never itself armed into
    spotter-ai; see the self-wake guard test), so these are mutually exclusive
    branches.
    """

    session = app.state.sessions.get(session_id)
    if session is None:
        return
    if session.approval_mode == SPOTTER_APPROVAL_MODE:
        wake_on_parent_activity(app, session_id)
        return
    parent_id = str(getattr(session, "parent_session_id", "") or "")
    if not parent_id:
        return
    task = next(
        (t for t in _live_watcher_tasks(app, parent_id) if t.child_session_id == session_id),
        None,
    )
    if task is None:
        return
    messages = app.state.messages.get(session_id, []) or []
    user_messages = [message for message in messages if getattr(message, "role", "") == "user"]
    latest_turn_id = (
        str(getattr(user_messages[-1], "turn_id", "") or getattr(user_messages[-1], "id", "") or "")
        if user_messages
        else ""
    )
    finals = [
        message
        for message in messages
        if getattr(message, "role", "") == "assistant"
        and not (getattr(message, "metadata", {}) or {}).get("live")
        and (not latest_turn_id or str(getattr(message, "turn_id", "") or "") == latest_turn_id)
    ]
    final = finals[-1] if finals else None
    error_info = getattr(final, "error_info", None) if final is not None else None
    if latest_turn_id and (final is None or error_info is not None):
        from clio_agent.gact.turn_spawn_failures import (  # noqa: PLC0415
            child_task_error_reason,
        )

        reason = child_task_error_reason(error_info)
        updated = _set_live_state(app, task.task_id, "error", error_reason=reason)
        event_name = "spotter_watcher_check_turn_failed"
    else:
        reason = ""
        updated = _set_live_state(app, task.task_id, "waiting")
        event_name = "spotter_watcher_check_turn_ended"
    if updated is not None:
        signal_clearance(app, parent_id)
        detail = f" reason={reason}" if reason else ""
        message = f"{event_name}{detail} session=%s watcher=%s task=%s"
        logger.info(message, parent_id, session_id, task.task_id)
        trace.event("SPOTTER", message, parent_id, session_id, task.task_id)
