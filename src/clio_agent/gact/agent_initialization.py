"""Failure reporting for deferred agent construction."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.providers.profile_store import ProviderProfileStore
from clio_agent.providers.lm_spec import spec_from_config


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

    Answers submitted while the agent is initializing stay durably queued, but a
    failed initialization must not leave their sessions looking idle forever.
    Publish a typed failure and mark only those affected sessions failed; a later
    successful provider bind still drains the retained inbox normally.
    """

    print(
        f"[clio-agent-gact] deferred agent {stage} failed ({exc!r}); "
        "POST /messages will keep returning 503.",
        flush=True,
    )
    app.state.agent_init_error = repr(exc)
    from clio_agent.gact.events import Event  # noqa: PLC0415

    for session_id, inbox in list(getattr(app.state, "loop_inboxes", {}).items()):
        deferred_questions = [
            str(event.metadata.get("question_id") or "")
            for event in inbox.snapshot()
            if event.kind == "user_message" and event.metadata.get("ask_user_resume")
        ]
        deferred_questions = [question_id for question_id in deferred_questions if question_id]
        if not deferred_questions:
            continue
        app.state.sessions.update(
            session_id,
            status="failed",
            metadata_patch={
                "deferred_resume_error": {
                    "reason": "agent_init_failed",
                    "stage": stage,
                    "question_ids": deferred_questions,
                }
            },
        )
        app.state.bus.publish(
            Event(
                type="user_question.resume_failed",
                session_id=session_id,
                payload={
                    "session_id": session_id,
                    "question_ids": deferred_questions,
                    "reason": "agent_init_failed",
                    "stage": stage,
                    "recoverable": True,
                },
            )
        )


def update_provider_profile(app: Any, agent: Any) -> None:
    """Reseed the app's default profile from the agent's resolved configuration."""

    existing = getattr(app.state, "provider_profiles", None)
    default_spec = spec_from_config(agent._provider_config)
    app.state.provider_profiles = (
        existing.with_default(default_spec)
        if isinstance(existing, ProviderProfileStore)
        else ProviderProfileStore.seed(default_spec)
    )
