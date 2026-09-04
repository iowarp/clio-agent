"""Readiness and failure reporting for deferred agent construction."""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.gact.providers.profile_store import ProviderProfileStore
from clio_agent.providers.lm_spec import spec_from_config

logger = logging.getLogger(__name__)

#: Typed reason stamped on every input refused while the agent is absent.
AGENT_INIT_FAILED_REASON = "agent_init_failed"

#: The ONE door that re-runs construction and re-drives these inboxes:
#: ``PUT /v1/providers/lm`` builds a fresh agent and calls
#: :func:`mark_agent_ready`. Named on the refusal so the client is told what
#: actually recovers the retained work, rather than "restart the server".
AGENT_INIT_RECOVERY_ACTIONS = ["rebind_lm_provider"]


def mark_agent_ready(app: Any, agent: Any) -> None:
    """Publish the live agent and promote inputs deferred during initialization."""

    app.state.agent = agent

    def drain() -> None:
        from clio_agent.gact.loop_inbox import drain_inbox_to_new_turn  # noqa: PLC0415

        for session_id in list(app.state.loop_inboxes):
            drain_inbox_to_new_turn(app, session_id)

    loop = getattr(app.state, "mcp_app_loop", None)
    if loop is not None and loop.is_running():
        loop.call_soon(drain)
    else:
        drain()


def record_init_failure(app: Any, exc: BaseException, *, stage: str) -> None:
    """Expose one typed deferred-construction failure without leaving partial state.

    While ``app.state.agent`` is ``None`` the ask-user resume parks answers on the
    loop inboxes, which only :func:`mark_agent_ready` drains. Left alone those
    answers sit there with their sessions reporting ``idle`` -- the user answered
    a question, the session looked ready, and nothing said otherwise.

    Construction failing is NOT terminal for the process, though: ``PUT
    /v1/providers/lm`` builds a fresh agent and calls :func:`mark_agent_ready`,
    which drains exactly these inboxes. So the honest report is refuse-and-RETAIN
    -- say per item that it will not be delivered now, name the door that still
    delivers it, and leave it queued for that door.
    """

    print(
        f"[clio-agent-gact] deferred agent {stage} failed ({exc!r}); "
        "POST /messages will keep returning 503.",
        flush=True,
    )
    app.state.agent_init_error = repr(exc)
    refuse_parked_inputs(app, stage=stage, detail=repr(exc))


def refuse_parked_inputs(app: Any, *, stage: str, detail: str) -> int:
    """Refuse — without destroying — every input parked for an absent agent.

    One ``session.input_refused`` event per parked item (never one summary for
    the batch: each is a distinct user intent that must be individually visible),
    and the owning session is surfaced ``error`` rather than the ``idle`` the
    defer path left behind. Returns the number of refused items.

    The item itself STAYS on its inbox, and the durable steer intent behind it
    stays ``pending``. Both are true: a provider rebind delivers this work, so
    settling the intent would publish a cancellation the recovery drain then
    contradicts, and draining the inbox would throw away an answer the user
    already gave. ``recoverable``/``retained`` on the payload say exactly that.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415

    inboxes = getattr(app.state, "loop_inboxes", None) or {}
    refused = 0
    for session_id, inbox in list(inboxes.items()):
        events = inbox.snapshot()
        if not events:
            continue
        for event in events:
            refused += 1
            app.state.bus.publish(
                Event(
                    type="session.input_refused",
                    session_id=session_id,
                    payload={
                        "session_id": session_id,
                        "reason": AGENT_INIT_FAILED_REASON,
                        "stage": stage,
                        "detail": detail,
                        "kind": event.kind,
                        "message_id": event.steer_message_id,
                        "task_id": event.task_id,
                        "question_id": str(event.metadata.get("question_id") or ""),
                        "recoverable": True,
                        "retained": True,
                        "recovery_actions": list(AGENT_INIT_RECOVERY_ACTIONS),
                    },
                )
            )
        logger.error(
            "parked inputs refused reason=%s session=%s stage=%s count=%d retained=%d",
            AGENT_INIT_FAILED_REASON,
            session_id,
            stage,
            len(events),
            len(events),
        )
        app.state.sessions.update(
            session_id,
            status="error",
            metadata_patch={
                "agent_init_error": {
                    "reason": AGENT_INIT_FAILED_REASON,
                    "stage": stage,
                    "detail": detail,
                    "retained_inputs": len(events),
                    "recovery_actions": list(AGENT_INIT_RECOVERY_ACTIONS),
                }
            },
        )
    return refused


def update_provider_profile(app: Any, agent: Any) -> None:
    """Reseed the app's default profile from the agent's resolved configuration."""

    existing = getattr(app.state, "provider_profiles", None)
    default_spec = spec_from_config(agent._provider_config)
    app.state.provider_profiles = (
        existing.with_default(default_spec)
        if isinstance(existing, ProviderProfileStore)
        else ProviderProfileStore.seed(default_spec)
    )
