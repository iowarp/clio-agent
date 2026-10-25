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


def memory_model_identity(role: MemoryRole) -> dict[str, str]:
    """``{"provider_id", "model_id", "source"}`` for ``role`` (``source``: fleet or default)."""
    raw = fleet_settings()["judges"].get(role)
    if raw:
        choice = JudgeModel.model_validate(raw)
        return {"provider_id": choice.provider_id, "model_id": choice.model_id, "source": "fleet"}
    from clio_agent.config import load_config_from_env  # noqa: PLC0415

    config = load_config_from_env()
    provider = str(getattr(config, "provider_id", "") or getattr(config, "provider", "") or "")
    return {"provider_id": provider, "model_id": str(config.model), "source": "server_default"}


def memory_model(role: MemoryRole) -> tuple[Any, Any, dict[str, str]]:
    """``(lm, adapter, identity)`` for one background-pass call."""
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


__all__ = ["MemoryRole", "memory_model", "memory_model_identity"]
