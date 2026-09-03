"""Readiness and failure reporting for deferred agent construction."""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.gact.providers.profile_store import ProviderProfileStore
from clio_agent.providers.lm_spec import spec_from_config

logger = logging.getLogger(__name__)

#: Typed reason stamped on every input released when the agent never arrives.
AGENT_INIT_FAILED_REASON = "agent_init_failed"


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

    Construction failing is terminal for this process: nothing will ever call
    :func:`mark_agent_ready`, which is the ONLY drain of the loop inboxes that
    ``user_question_resume`` parks answers into while ``app.state.agent`` is
    ``None``. Those answers used to sit there forever with their sessions
    reporting ``idle`` -- the user answered a question and the session looked
    ready, with nothing left in the process that could ever deliver it. Every
    parked input is therefore released as a typed refusal here.
    """

    print(
        f"[clio-agent-gact] deferred agent {stage} failed ({exc!r}); "
        "POST /messages will keep returning 503.",
        flush=True,
    )
    app.state.agent_init_error = repr(exc)
    refuse_parked_inputs(app, stage=stage, detail=repr(exc))


def refuse_parked_inputs(app: Any, *, stage: str, detail: str) -> int:
    """Release every input parked for an agent that will never arrive.

    One ``session.input_refused`` event per stranded item (never one summary for
    the batch: each is a distinct user intent that must be individually visible),
    and the owning session is surfaced ``error`` rather than the ``idle`` the
    defer path left behind. Returns the number of refused items.
    """

    from clio_agent.gact.events import Event  # noqa: PLC0415

    inboxes = getattr(app.state, "loop_inboxes", None) or {}
    refused = 0
    for session_id, inbox in list(inboxes.items()):
        events = inbox.drain()
        if not events:
            continue
        for event in events:
            refused += 1
            _release_intent(app, session_id, event)
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
                        "recoverable": False,
                        "recovery_actions": ["fix_provider_configuration", "restart_server"],
                    },
                )
            )
        logger.error(
            "parked inputs refused reason=%s session=%s stage=%s count=%d",
            AGENT_INIT_FAILED_REASON,
            session_id,
            stage,
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
                    "refused_inputs": len(events),
                }
            },
        )
    return refused


def _release_intent(app: Any, session_id: str, event: Any) -> None:
    """Settle the durable steer intent behind a refused input, when it has one."""

    message_id = getattr(event, "steer_message_id", "")
    intents = getattr(app.state, "message_intents", None)
    if not message_id or intents is None:
        return
    # ``cancel_pending`` is the existing terminal transition for an accepted but
    # undelivered steer; reusing it keeps the intent ledger's own vocabulary
    # rather than inventing a second settled state for this one case.
    intents.cancel_pending(session_id, message_id)


def update_provider_profile(app: Any, agent: Any) -> None:
    """Reseed the app's default profile from the agent's resolved configuration."""

    existing = getattr(app.state, "provider_profiles", None)
    default_spec = spec_from_config(agent._provider_config)
    app.state.provider_profiles = (
        existing.with_default(default_spec)
        if isinstance(existing, ProviderProfileStore)
        else ProviderProfileStore.seed(default_spec)
    )
