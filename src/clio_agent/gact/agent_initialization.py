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
    """Expose one typed deferred-construction failure without leaving partial state."""

    print(
        f"[clio-agent-gact] deferred agent {stage} failed ({exc!r}); "
        "POST /messages will keep returning 503.",
        flush=True,
    )
    app.state.agent_init_error = repr(exc)


def update_provider_profile(app: Any, agent: Any) -> None:
    """Reseed the app's default profile from the agent's resolved configuration."""

    existing = getattr(app.state, "provider_profiles", None)
    default_spec = spec_from_config(agent._provider_config)
    app.state.provider_profiles = (
        existing.with_default(default_spec)
        if isinstance(existing, ProviderProfileStore)
        else ProviderProfileStore.seed(default_spec)
    )
