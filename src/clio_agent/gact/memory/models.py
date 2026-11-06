"""The models of the background pass: the fleet roles ``memory_extract`` and
``memory_consolidate``.

The operator picks them once for the whole fleet (Settings → Fleet). There is no
caller to inherit from in a background pass, so an unset role uses the server's
default model (the configured ``lm`` provider and model), never a session's.
"""

from __future__ import annotations

from typing import Any, Literal

from clio_agent.gact.fleet_settings import JudgeModel, fleet_settings, model_route

MemoryRole = Literal["memory_extract", "memory_consolidate"]
ROLES: tuple[MemoryRole, ...] = ("memory_extract", "memory_consolidate")
#: Typed reason: a role has no fleet model and no LM provider was selected, so the
#: server default would be the unselected ``lm_studio`` base default.
MEMORY_MODEL_UNCONFIGURED = "lm_provider_unconfigured"


def unconfigured_roles() -> tuple[MemoryRole, ...]:
    """The pass's roles with neither a fleet model nor a selected LM provider."""
    from clio_agent.gact.providers.boot_selection import explicit_lm_provider  # noqa: PLC0415

    if explicit_lm_provider() or _host_agent_config() is not None:
        return ()
    judges = fleet_settings()["judges"]
    return tuple(role for role in ROLES if not judges.get(role))


class MemoryModelUnconfigured(ValueError):
    """No fleet role and no server default model: the background pass has nothing to run on."""

    error_type = "memory_model_unconfigured"


def _host_agent_config() -> Any:
    """The running host agent's LM config (a saved model server keeps its ``server_id``)."""
    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    agent = getattr(getattr(app, "state", None), "agent", None)
    return getattr(agent, "_provider_config", None)


def _server_default_config() -> Any:
    """The server's running LM config: its host agent's, else an explicitly selected one.

    The host agent is the server default whether it was built at boot from the
    selected provider or from a session's own model (``session_host_agent``) -- a
    saved model server keeps its ``server_id`` there. With neither, the committed
    ``lm_studio`` base default is not a selection: refuse (typed) rather than retry
    LM Studio on ``127.0.0.1:1234`` per call.
    """
    config = _host_agent_config()
    if config is not None:
        return config
    # ``memory_model`` refuses an unconfigured role first (``unconfigured_roles``).
    from clio_agent.config import load_config_from_env  # noqa: PLC0415

    return load_config_from_env()


def memory_model_identity(role: MemoryRole) -> dict[str, str]:
    """``{"provider_id", "model_id", "source"}`` for ``role`` (``source``: fleet or default).

    Raises:
        MemoryModelUnconfigured: No fleet choice and no server default model.
    """
    raw = fleet_settings()["judges"].get(role)
    if raw:
        choice = JudgeModel.model_validate(raw)
        return {"provider_id": choice.provider_id, "model_id": choice.model_id, "source": "fleet"}
    config = _server_default_config()
    provider = str(
        getattr(config, "server_id", "")
        or getattr(config, "provider_id", "")
        or getattr(config, "provider", "")
        or ""
    )
    return {"provider_id": provider, "model_id": str(config.model), "source": "server_default"}


def memory_model(role: MemoryRole) -> tuple[Any, Any, dict[str, str]]:
    """``(lm, adapter, identity)`` for one background-pass call.

    Raises:
        ClioError: ``lm_provider_unconfigured`` when the role has no model to run on;
            the unselected base default is never probed.
    """
    if role in unconfigured_roles():
        from clio_agent.errors import ClioError  # noqa: PLC0415

        raise ClioError(
            f"No model is configured for {role}: select an LM provider or a fleet model.",
            error_type=MEMORY_MODEL_UNCONFIGURED,
            details={"reason": MEMORY_MODEL_UNCONFIGURED, "role": role},
        )
    identity = memory_model_identity(role)
    if role == "memory_consolidate":
        from clio_agent.gact import context  # noqa: PLC0415

        app, sid = context.active_app(), context.active_session_id()
        session = app.state.sessions.get(sid) if app is not None and sid else None
        run_id = str((getattr(session, "metadata", None) or {}).get("memory_creation_run_id") or "")
        if run_id:
            from clio_agent.gact.memory.creation_runs import update_run  # noqa: PLC0415
            from clio_agent.gact.memory.runtime import memory_service  # noqa: PLC0415

            update_run(memory_service(app).store, run_id, {"models": {"curator": identity}})
    lm, adapter = model_route(identity["provider_id"], identity["model_id"])
    return lm, adapter, identity


__all__ = [
    "MEMORY_MODEL_UNCONFIGURED",
    "MemoryModelUnconfigured",
    "MemoryRole",
    "memory_model",
    "memory_model_identity",
    "unconfigured_roles",
]
