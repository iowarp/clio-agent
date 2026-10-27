"""Readiness and failure reporting for deferred agent construction."""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.gact.providers.profile_store import ProviderProfileStore
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.providers.lm_spec import spec_from_config

logger = logging.getLogger(__name__)

#: Typed reason stamped on every input refused while the agent is absent.
AGENT_INIT_FAILED_REASON = "agent_init_failed"

#: The ONE door that re-runs construction and re-drives these inboxes:
#: ``PUT /v1/providers/lm`` builds a fresh agent and calls
#: :func:`mark_agent_ready`. Named on the refusal so the client is told what
#: actually recovers the retained work, rather than "restart the server".
AGENT_INIT_RECOVERY_ACTIONS = ["rebind_lm_provider"]


def agent_not_available_error(app: Any, sid: str) -> ErrorEnvelope:
    """Return a typed error when no executable CLIO agent is ready for a turn."""

    task = getattr(app.state, "agent_construction_task", None)
    task_done = bool(getattr(task, "done", lambda: True)())
    init_error = str(getattr(app.state, "agent_init_error", "") or "")
    want_agent = bool(getattr(app.state, "want_agent", False))

    if want_agent and not task_done:
        status = "starting"
        message = "CLIO is still starting its agent; no agent is ready to accept messages yet."
        recoverable = True
        recovery_actions = ["wait_for_agent_startup", "retry", "check_health"]
    elif init_error:
        status = "failed"
        message = "CLIO agent startup failed; no agent is available to accept messages."
        recoverable = True
        recovery_actions = ["check_server_logs", "fix_lm_configuration", "restart_agent"]
    else:
        status = "not_configured"
        # A message naming a model builds the host (session_host_agent); this one named none.
        message = "No model is selected for this session. Choose a model, then send again."
        recoverable = True
        recovery_actions = ["select_session_model", "retry"]

    details: dict[str, Any] = {
        "session_id": sid,
        "agent_status": status,
        "want_agent": want_agent,
        "recovery_actions": recovery_actions,
    }
    if init_error:
        details["agent_init_error"] = init_error

    return ErrorEnvelope(
        error=ErrorInfo(
            error="agent_not_available",
            message=message,
            details=details,
            recoverable=recoverable,
        )
    )


def mark_agent_ready(app: Any, agent: Any, *, boot: bool = False) -> None:
    """Publish the live agent and promote inputs deferred during initialization.

    ``boot``: the agent was built from the persisted selection, not a bind, so its
    context window is discovered in the background (F042).
    """

    app.state.agent = agent
    if boot:
        from clio_agent.gact import boot_timing  # noqa: PLC0415
        from clio_agent.gact.providers import boot_handshake  # noqa: PLC0415

        boot_timing.mark(app, "agent_ready")

        boot_handshake.schedule(app, agent)

    def drain() -> None:
        owner = getattr(app.state, "conversations", None)
        if owner is not None:
            owner.signal_pending()
        from clio_agent.gact.session_warmup import start_deferred_warmups  # noqa: PLC0415

        start_deferred_warmups(app)  # conversations opened while the agent was starting

    loop = getattr(app.state, "mcp_app_loop", None)
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(drain)
    else:
        drain()


def record_init_failure(app: Any, exc: BaseException, *, stage: str) -> None:
    """Expose one typed deferred-construction failure without leaving partial state.

    While ``app.state.agent`` is ``None``, confirmed Core inputs remain pending
    until :func:`mark_agent_ready` rechecks common conversation admission. Left alone those
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
    # The readiness gate retains Core mail. An initialization failure cannot
    # withdraw or acknowledge already accepted human/task input.


def update_provider_profile(app: Any, agent: Any) -> None:
    """Reseed the app's default profile from the agent's resolved configuration."""

    existing = getattr(app.state, "provider_profiles", None)
    default_spec = spec_from_config(agent._provider_config)
    app.state.provider_profiles = (
        existing.with_default(default_spec)
        if isinstance(existing, ProviderProfileStore)
        else ProviderProfileStore.seed(default_spec)
    )
