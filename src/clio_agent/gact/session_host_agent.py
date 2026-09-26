"""Build the runtime host from a session's own model selection, on its first message.

The server builds its host agent (``ClioAgent``: tool fleet, ARC keystone,
registry) at boot only for a provider the user explicitly selected. On a fresh
install there is none (the committed ``lm_studio`` default is not a selection),
so the process runs agent-less. Picking a model in the composer is nevertheless
a complete choice: the message carries it as its model reference (or the session
carries it as its default), and every turn already runs on that reference
(``turn_forward._apply_turn_model_selection``). What was missing was the host
itself, so the first turn of such a session was refused with
``agent_not_available`` and the client papered over it by demanding a global
"apply in Settings" step first.

:func:`ensure_host_agent` closes that: when no host exists and the message (or
its session) names a model, it builds the host for exactly that provider/model
-- once, under a lock, reusing an in-flight boot construction -- and publishes it
the way a provider bind does. It does NOT record a global provider selection:
the health row keeps reporting ``lm_provider_unconfigured`` and nothing is
written to the config file. A build failure is recorded as the typed
``agent_init_error`` the message route already reports.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from clio_agent.gact import agent_initialization
from clio_agent.gact.relay_wiring import construct_agent_with_relay
from clio_agent.gact.server_boot import process_arc_off_loop

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.config import LMProviderConfig
    from clio_agent.gact.message_contract import PostMessageRequest
    from clio_agent.gact.types import ModelRef

logger = logging.getLogger(__name__)

__all__ = ["ensure_host_agent"]


def _selected_ref(app: "FastAPI", sid: str, req: "PostMessageRequest") -> "ModelRef | None":
    """The message's model reference, else its session's; ``None`` when neither names one."""

    from clio_agent.gact.providers.config import _model_ref_dict  # noqa: PLC0415
    from clio_agent.gact.types import ModelRef  # noqa: PLC0415

    session = app.state.sessions.get(sid)
    for candidate in (req.model, getattr(session, "model", None)):
        if candidate is None:
            continue
        ref = ModelRef(**_model_ref_dict(candidate))
        if ref.provider_id.strip() and ref.model_id.strip():
            return ref
    return None


def _host_config(ref: "ModelRef") -> "LMProviderConfig":
    """The provider config the host is built with: exactly the selected reference."""

    from clio_agent.config import LMProviderConfig  # noqa: PLC0415
    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    preset = get_provider(ref.provider_id)
    variant = ref.variant.strip() if preset is not None and preset.provider_kind == "codex" else ""
    return LMProviderConfig(
        provider_id=ref.provider_id,
        model=ref.model_id,
        codex_variant=variant,  # type: ignore[arg-type]  # LMProviderConfig validates
    )


def _lock(app: "FastAPI") -> asyncio.Lock:
    lock = getattr(app.state, "session_host_agent_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.session_host_agent_lock = lock
    return lock


def _publish(app: "FastAPI", agent: Any) -> None:
    """Publish a freshly built host exactly as a provider bind publishes one."""

    from clio_agent.gact.runtime.ambient_lm import install_process_default_lm  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _set_app_arc  # noqa: PLC0415
    from clio_agent.gact.tool_observer import _install_tool_runtime_hooks  # noqa: PLC0415

    agent_initialization.update_provider_profile(app, agent)
    install_process_default_lm(
        getattr(agent, "_main_lm", None), getattr(agent, "_dspy_adapter", None)
    )
    if getattr(agent, "arc", None) is not None:
        _set_app_arc(app, agent.arc)
    app.state.agent_init_error = ""
    agent_initialization.mark_agent_ready(app, agent)
    _install_tool_runtime_hooks(app)


async def ensure_host_agent(app: "FastAPI", sid: str, req: "PostMessageRequest") -> None:
    """Build the host for this message's selected model when the server has none.

    A no-op when a host exists or nothing names a model (the route then reports
    ``agent_not_available`` as before). Never raises: a failed build is recorded
    through :func:`agent_initialization.record_init_failure`, which the route turns
    into a typed 503 carrying ``agent_init_error``.

    Args:
        app: The serving GACT application.
        sid: The session the message is for.
        req: The incoming message request.
    """

    if app.state.agent is not None:
        return
    ref = _selected_ref(app, sid, req)
    if ref is None:
        return
    async with _lock(app):
        boot = getattr(app.state, "agent_construction_task", None)
        if boot is not None and not boot.done():
            await asyncio.shield(boot)  # a boot build in flight is the host; share it
        if app.state.agent is not None:
            return
        try:
            cfg = _host_config(ref)
            agent = await construct_agent_with_relay(
                app, arc=await process_arc_off_loop(app), provider_config=cfg
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as the typed agent_init_error
            logger.warning(
                "session host agent build failed reason=session_host_agent_failed "
                "session=%s provider=%s model=%s error=%r",
                sid,
                ref.provider_id,
                ref.model_id,
                exc,
            )
            agent_initialization.record_init_failure(app, exc, stage="session_host")
            return
        _publish(app, agent)
        logger.info(
            "session host agent built reason=session_model_selection session=%s "
            "provider=%s model=%s variant=%s",
            sid,
            ref.provider_id,
            ref.model_id,
            ref.variant,
        )
