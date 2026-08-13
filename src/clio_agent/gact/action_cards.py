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
appends it to the session's live transcript via
:func:`clio_agent.gact.tool_observer._append_live_assistant_part`, which is
TURN-AGNOSTIC — it publishes through the active turn's ledger when one is running,
or opens/reuses a live assistant message and publishes ``message.part.added``
directly when the target session has no in-flight turn (the common case for a
watcher notifying an idle parent). Either way the caller never needs to know
whether the target session currently has a live turn.

``raise_alert_card`` (:func:`build_raise_alert_card_tool`) is auto-attached to
EVERY react tool-using expert exactly like ``create_artifact`` / ``load_skill``
(:mod:`clio_agent.gact.agents.auto_tools`) — NOT counted against the 5-7 curated
domain-tool budget, and available regardless of whether an expert's OWN curated
``tools:`` list declares it. This is deliberate: any spawned child (spotter's
watcher included) needs the ability to notify its parent without every blueprint
author having to remember to declare it.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.parts import Part

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.agent_tasks import AgentTask

#: MVP card lifecycle values (the shared ``status`` field-group slot). Open on the
#: wire — a future "resolved" value is legal even before it lands here — but the
#: builder pins the shipped default so callers never have to remember it.
DEFAULT_ACTION_CARD_STATUS = "active"

#: Typed error reason returned by ``raise_alert_card`` (never a silent no-op) when
#: the calling session is not itself a spawned child with a live parent AgentTask.
ALERT_CARD_NO_PARENT_ERROR = "alert_card_no_parent"


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


def emit_action_card_part(app: "FastAPI", session_id: str, part: Part) -> Part:
    """Append one ``action_card`` part to ``session_id``'s live transcript.

    Turn-agnostic (see module docstring): works identically whether ``session_id``
    currently has an in-flight turn or not.

    Args:
        app: The GACT app (transcript/message-bus state lives on ``app.state``).
        session_id: The session the card should appear in (for the spotter watcher
            this is the PARENT session, never the watcher's own child session).
        part: The card part, normally built by :func:`action_card_part`.

    Returns:
        The same ``part`` that was appended, for convenience chaining.
    """

    from clio_agent.gact.tool_observer import _append_live_assistant_part  # noqa: PLC0415

    _append_live_assistant_part(app, session_id, part)
    return part


def _calling_task(app: "FastAPI", session_id: str) -> Optional["AgentTask"]:
    """The calling session's OWN :class:`~clio_agent.gact.agent_tasks.AgentTask` row.

    Looked up by ``child_session_id == session_id`` over its parent's task index
    (``AgentTaskRegistry.for_parent``) — the cheap, indexed lookup, since a spawned
    child always knows its own ``parent_session_id``. Returns ``None`` when
    ``session_id`` has no parent (a bare/root session, never spawned) or the
    registry has no matching row (a race with the spawn record, or a session
    predating the agent-task projection).
    """

    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return None
    session = app.state.sessions.get(session_id)
    parent_id = str(getattr(session, "parent_session_id", "") or "") if session is not None else ""
    if not parent_id:
        return None
    return next(
        (task for task in registry.for_parent(parent_id) if task.child_session_id == session_id),
        None,
    )


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
            card's ``source``/``agent_id`` attribution.

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
        stub_actions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Raise a notification/action card into YOUR PARENT session's transcript.

        Use this when you (a spawned child agent) discover something your parent
        session's user should see WITHOUT waiting for you to finish and be waited
        on — e.g. an anomaly, a quarantine, a blocking finding. ``severity`` is
        ``info`` | ``warning`` | ``critical``. The card always carries a "Discuss"
        action that focuses your task in the parent's UI. Pass ``stub_actions`` as
        ``[{"id", "label", "reason"}, ...]`` for future actions you want the UI to
        show but not yet wire (rendered disabled, with ``reason`` as the tooltip).

        Only callable from a SPAWNED CHILD session (one with a live parent
        AgentTask) — calling this from a top-level/unspawned session returns a
        typed error instead of silently doing nothing.
        """

        app = _ctx.active_app()
        sid = _ctx.active_session_id()
        task = _calling_task(app, sid) if app is not None and sid else None
        if task is None:
            return {
                "error": ALERT_CARD_NO_PARENT_ERROR,
                "message": (
                    "raise_alert_card requires a spawned-child session with a live "
                    "parent AgentTask; this session has neither."
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
        for entry in list(stub_actions or []):
            actions.append(
                {
                    "id": str(entry.get("id") or ""),
                    "label": str(entry.get("label") or ""),
                    "enabled": False,
                    "behavior": {
                        "kind": "stub",
                        "reason": str(entry.get("reason") or ""),
                    },
                }
            )

        part = action_card_part(
            source=caller_expert_id,
            severity=severity,
            title=title,
            body=body,
            actions=actions,
            agent_id=caller_expert_id,
        )
        emit_action_card_part(app, task.parent_session_id, part)
        return {"emitted": True, "session_id": task.parent_session_id, "part_id": part.id}

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
                "description": (
                    'Optional future actions to show disabled: [{"id", "label", "reason"}, ...].'
                ),
            },
        },
    )
