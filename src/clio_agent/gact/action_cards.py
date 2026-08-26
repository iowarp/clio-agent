"""``action_card`` part builder + emitter — a GENERIC in-transcript notification
primitive (frozen wire contract, SPOTTER MVP week, owner ruling).

Spotter-ai is the FIRST emitter (a spawned watcher child raising an alert into its
parent session's transcript via the ``raise_alert_card`` native tool built here,
:func:`build_raise_alert_card_tool`), but nothing here is spotter-specific: any
caller may build and emit a card, and any spawned child agent may call the tool.
Permission/HITL cards are designed to migrate onto this same primitive later (not
this week — see the frozen contract).

Mirrors :mod:`clio_agent.gact.background_exit`'s ``*_part`` / ``emit_*_part`` split:
:func:`action_card_part` is a pure builder (no I/O); :func:`emit_action_card_part`
appends it to the session's live transcript, TURN-AGNOSTIC — through the active
turn's ledger when one is running, or via the legacy live-parts fallback when the
target session has no in-flight turn (the common case for a watcher notifying an
idle parent). It reports whether the append actually landed: a FROZEN (already
settled/abandoned) ledger drops a late append typed rather than silently
swallowing it (no-silent-fallback ground rule), and the caller (``raise_alert_card``)
surfaces that as a typed ``emitted: false`` result instead of lying about success.

``raise_alert_card`` (:func:`build_raise_alert_card_tool`) is auto-attached to
EVERY react tool-using expert exactly like ``create_artifact`` / ``load_skill``
(:mod:`clio_agent.gact.agents.auto_tools`) — NOT counted against the 5-7 curated
domain-tool budget, and available regardless of whether an expert's OWN curated
``tools:`` list declares it. This is deliberate: any spawned child (spotter's
watcher included) needs the ability to notify its parent without every blueprint
author having to remember to declare it.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.parts import Part
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.agent_tasks import AgentTask

logger = logging.getLogger(__name__)

#: MVP card lifecycle values (the shared ``status`` field-group slot). Open on the
#: wire — a future "resolved" value is legal even before it lands here — but the
#: builder pins the shipped default so callers never have to remember it.
DEFAULT_ACTION_CARD_STATUS = "active"

#: Typed error reasons returned by ``raise_alert_card`` (never a silent no-op).
#: The calling session has no ``parent_session_id`` at all (a bare/root session,
#: never spawned).
ALERT_CARD_NO_PARENT_ERROR = "alert_card_no_parent"
#: The calling session DOES have a parent, but the agent-task registry has no
#: matching row for it (a race with the spawn record, or a session predating
#: the agent-task projection) — a different reality from having no parent at all.
ALERT_CARD_TASK_ROW_MISSING_ERROR = "alert_card_task_row_missing"
#: The card was built but the parent's live transcript ledger was already
#: FROZEN (settled/abandoned) by the time the append reached it.
ALERT_CARD_PARENT_TRANSCRIPT_FROZEN = "parent_transcript_frozen"

#: A bare-string stub action shorthand: ``"address"`` -> a disabled "Address"
#: button. Its reason is always this fixed, honest placeholder — a model
#: passing a bare string is declaring "I want this button to exist" without
#: authoring a specific tooltip.
_BARE_STUB_ACTION_REASON = "not yet implemented"


def action_card_part(
    *,
    source: str,
    severity: str,
    title: str,
    body: str,
    actions: list[dict[str, Any]] | None = None,
    status: str = DEFAULT_ACTION_CARD_STATUS,
    agent_id: str = "",
) -> Part:
    """Build one ``action_card`` :class:`~clio_agent.gact.parts.Part`.

    Args:
        source: Emitter identity, a free string (e.g. ``"spotter-ai"``; future:
            a tool name).
        severity: An open string; MVP values are ``"info" | "warning" | "critical"``.
        title: Headline text (e.g. "SPOTTER AI has detected an issue").
        body: Detail text (markdown allowed, rendered like other part bodies).
        actions: A list of ``{id, label, enabled, behavior}`` action objects. Each
            ``behavior`` is an open discriminated union on ``kind`` — unknown kinds
            must render as a disabled button on the client, never crash it. Empty
            list (the default) omits the field entirely on the wire.
        status: Card lifecycle value; MVP always ``"active"``.
        agent_id: The expert/agent id attributed as the part's generator. Defaults
            to empty (server-authored parts with no single generating expert).

    Returns:
        A :class:`~clio_agent.gact.parts.Part` ready for
        :func:`emit_action_card_part` (or any other transcript-append path).
    """

    return Part(
        id=f"live_action_card_{uuid.uuid4().hex[:12]}",
        type="action_card",
        agent_id=agent_id,
        source=source,
        severity=severity,
        title=title,
        body=body,
        status=status,
        actions=list(actions or []),
    )


def emit_action_card_part(app: "FastAPI", session_id: str, part: Part) -> bool:
    """Append one ``action_card`` part to ``session_id``'s live transcript.

    Turn-agnostic (see module docstring): works identically whether ``session_id``
    currently has an in-flight turn or not — EXCEPT a live turn whose ledger is
    already FROZEN (settled/abandoned mid-race), which drops the append. That
    drop is never silent: logged typed here, and surfaced to the caller via the
    return value (never a lie-by-omission ``True``).

    Args:
        app: The GACT app (transcript/message-bus state lives on ``app.state``).
        session_id: The session the card should appear in (for the spotter watcher
            this is the PARENT session, never the watcher's own child session).
        part: The card part, normally built by :func:`action_card_part`.

    Returns:
        ``True`` when the part actually landed (either appended into a live,
        non-frozen turn ledger, or queued via the no-active-turn fallback, which
        always succeeds); ``False`` when a live turn's ledger was frozen and the
        append was dropped.
    """

    from clio_agent.gact.tool_observer import (  # noqa: PLC0415
        _append_live_assistant_part,
        _mirror_transcript_state,
        _session_turn_transcript,
    )

    transcript = _session_turn_transcript(app, session_id)
    if transcript is not None:
        # action_card is a plain atomic part (never expert_handoff / a collector
        # tool_call|tool_result), so it always takes append_part's plain path --
        # called DIRECTLY here (not via _append_live_assistant_part) so this
        # function can observe append_part's own Optional[Part] frozen signal,
        # which _append_live_assistant_part's shared dispatch discards.
        appended = transcript.append_part(part)
        if appended is None:
            logger.warning(
                "action_card_dropped reason=%s session=%s part_type=%s",
                ALERT_CARD_PARENT_TRANSCRIPT_FROZEN,
                session_id,
                part.type,
            )
            trace.event(
                "SPOTTER",
                "action_card_dropped reason=%s session=%s part_type=%s",
                ALERT_CARD_PARENT_TRANSCRIPT_FROZEN,
                session_id,
                part.type,
            )
            return False
        _mirror_transcript_state(app, session_id, transcript)
        return True

    # No active turn ledger: the legacy live-parts fallback always succeeds
    # (there is no frozen state to race against off-turn).
    _append_live_assistant_part(app, session_id, part)
    return True


def _calling_task(app: "FastAPI", session_id: str) -> tuple[Optional["AgentTask"], str]:
    """The calling session's OWN :class:`~clio_agent.gact.agent_tasks.AgentTask` row.

    Looked up by ``child_session_id == session_id`` over its parent's task index
    (``AgentTaskRegistry.for_parent``) — the cheap, indexed lookup, since a spawned
    child always knows its own ``parent_session_id``.

    Returns:
        ``(task, "")`` on success. On failure, two DISTINCT typed realities:
        ``(None, "no_parent_session")`` when ``session_id`` has no
        ``parent_session_id`` at all (a bare/root session, never spawned), or
        ``(None, "task_row_missing")`` when it DOES have a parent but the
        registry has no matching row (a race with the spawn record, or a
        session predating the agent-task projection).
    """

    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return None, "task_row_missing"
    session = app.state.sessions.get(session_id)
    parent_id = str(getattr(session, "parent_session_id", "") or "") if session is not None else ""
    if not parent_id:
        return None, "no_parent_session"
    task = next(
        (t for t in registry.for_parent(parent_id) if t.child_session_id == session_id), None
    )
    if task is None:
        return None, "task_row_missing"
    return task, ""


def _coerce_stub_actions(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Coerce a model-supplied ``stub_actions`` argument into wire action rows.

    Never raises: a model passing bare strings (``["address", "remove"]``) or a
    single non-list value is a REAL, observed shape (not a hypothetical), so
    this is a format-only, no-semantic-change adapter (allowed per the "surface
    reality, never silently fix" ground rule) — not case-logic deciding
    anything about the alert itself.

    Args:
        raw: Whatever the model passed for ``stub_actions`` (ideally a list of
            ``{id, label, reason}`` dicts, but tolerantly also bare strings or a
            single non-list entry).

    Returns:
        ``(actions, skipped)`` — ``actions`` are wire-ready ``{id, label,
        enabled, behavior}`` rows (each disabled, ``behavior.kind == "stub"``);
        ``skipped`` lists any entry that could not be coerced, each
        ``{"value": repr(entry), "reason": "unsupported_stub_action_type"}``, so
        a caller can see EXACTLY what was dropped rather than have it vanish.
    """

    if raw is None:
        entries: list[Any] = []
    elif isinstance(raw, list):
        entries = raw
    else:
        # A bare single entry (str or dict) -- tolerate rather than crash on
        # list(raw), which would character-split a bare string.
        entries = [raw]

    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for entry in entries:
        if isinstance(entry, str):
            actions.append(
                {
                    "id": entry,
                    "label": entry.title(),
                    "enabled": False,
                    "behavior": {"kind": "stub", "reason": _BARE_STUB_ACTION_REASON},
                }
            )
        elif isinstance(entry, dict):
            actions.append(
                {
                    "id": str(entry.get("id") or ""),
                    "label": str(entry.get("label") or ""),
                    "enabled": False,
                    "behavior": {"kind": "stub", "reason": str(entry.get("reason") or "")},
                }
            )
        else:
            skipped.append({"value": repr(entry), "reason": "unsupported_stub_action_type"})
    return actions, skipped


def build_raise_alert_card_tool(agent_def: Any) -> Any:
    """Build the ``raise_alert_card`` dspy.Tool (auto-attached runtime infrastructure).

    Attached to EVERY react tool-using expert alongside ``create_artifact`` /
    ``load_skill`` (see :mod:`clio_agent.gact.agents.auto_tools`) — a GENERIC way
    for any spawned child agent to raise a notification/action card into its
    PARENT session's transcript. Nothing here is spotter-specific; spotter-ai's
    watcher is simply the first caller.

    Args:
        agent_def: The resolved :class:`~clio_agent.gact.types.AgentDef` for the
            expert this tool is being attached to — its id is stamped as the
            card's ``agent_id`` attribution (and as ``source``'s fallback — see
            the returned tool's own docstring).

    Returns:
        A ``dspy.Tool`` wrapping :func:`raise_alert_card` bound to ``agent_def``.
    """

    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    caller_expert_id = str(getattr(agent_def, "id", "") or "")

    def raise_alert_card(
        title: str,
        body: str,
        severity: str = "warning",
        stub_actions: Optional[list[Any]] = None,
    ) -> dict[str, Any]:
        """Raise a notification/action card into YOUR PARENT session's transcript.

        Use this when you (a spawned child agent) discover something your parent
        session's user should see WITHOUT waiting for you to finish and be waited
        on — e.g. an anomaly, a quarantine, a blocking finding. ``severity`` is
        ``info`` | ``warning`` | ``critical``. The card always carries a "Discuss"
        action that focuses your task in the parent's UI. Pass ``stub_actions`` as
        ``[{"id", "label", "reason"}, ...]`` for future actions you want the UI to
        show but not yet wire (rendered disabled, with ``reason`` as the tooltip);
        bare strings (``["address", "remove"]``) work too as a shorthand.

        Only callable from a SPAWNED CHILD session (one with a live parent
        AgentTask) — calling this from a top-level/unspawned session returns a
        typed error instead of silently doing nothing.
        """

        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        task, fail_reason = (
            _calling_task(app, sid) if app is not None and sid else (None, "no_parent_session")
        )
        if task is None:
            error = (
                ALERT_CARD_NO_PARENT_ERROR
                if fail_reason == "no_parent_session"
                else ALERT_CARD_TASK_ROW_MISSING_ERROR
            )
            logger.warning("raise_alert_card_dropped reason=%s session=%s", error, sid)
            trace.event("SPOTTER", "raise_alert_card_dropped reason=%s session=%s", error, sid)
            return {
                "error": error,
                "message": (
                    "raise_alert_card requires a spawned-child session with a live "
                    "parent AgentTask; this session has "
                    + (
                        "no parent session at all."
                        if error == ALERT_CARD_NO_PARENT_ERROR
                        else "a parent, but no matching AgentTask row."
                    )
                ),
            }

        actions: list[dict[str, Any]] = [
            {
                "id": "discuss",
                "label": "Discuss",
                "enabled": True,
                "behavior": {"kind": "focus_session", "handle_id": task.task_id},
            }
        ]
        stub_rows, skipped = _coerce_stub_actions(stub_actions)
        actions.extend(stub_rows)

        # A live task only ever comes out of _calling_task(app, sid), so app is
        # narrowed non-None here (the None case early-returned above).
        assert app is not None

        # Branded emitter identity: the calling session's OWN activated Agent
        # Blueprint (e.g. "spotter-ai"), never the bare expert id -- the card
        # header reads as the PRODUCT ("SPOTTER AI"), not an internal expert
        # name. Falls back to the expert id only when no blueprint is activated
        # (e.g. a bare/loose expert running without one).
        caller_session = app.state.sessions.get(sid)
        blueprint_id = str(
            (getattr(caller_session, "metadata", None) or {}).get("active_agent_blueprint_id") or ""
        )
        source = blueprint_id or caller_expert_id

        part = action_card_part(
            source=source,
            severity=severity,
            title=title,
            body=body,
            actions=actions,
            agent_id=caller_expert_id,
        )
        emitted = emit_action_card_part(app, task.parent_session_id, part)
        result: dict[str, Any] = (
            {"emitted": True, "session_id": task.parent_session_id, "part_id": part.id}
            if emitted
            else {
                "emitted": False,
                "reason": ALERT_CARD_PARENT_TRANSCRIPT_FROZEN,
                "session_id": task.parent_session_id,
            }
        )
        if skipped:
            result["skipped_stub_actions"] = skipped
        return result

    return native_tool(
        raise_alert_card,
        name="raise_alert_card",
        desc=raise_alert_card.__doc__,
        title="Raise Alert Card",
        args={
            "title": {"type": "string", "description": "Card headline."},
            "body": {
                "type": "string",
                "description": "Card detail text (markdown allowed).",
            },
            "severity": {
                "type": "string",
                "description": "One of info|warning|critical. Defaults to warning.",
            },
            "stub_actions": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Optional future actions to show disabled: "
                    '[{"id", "label", "reason"}, ...] (bare strings also accepted).'
                ),
            },
        },
    )
