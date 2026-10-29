"""Run a resolved agent outside a turn (slash commands that name an agent)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.claude_code_precede import precede_connect_claude_code_session

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef


def run_resolved_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
    images: list[Any] | None = None,
    files: list[Any] | None = None,
) -> Any:
    """Build ``agent_def``'s module from its pinned resolution and run it once."""

    from clio_agent.gact.agents.builders import build_agent_module  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        plugin_service,
    )

    digest = str(agent_def.metadata.get("resolution_digest") or "")
    resolution = plugin_service(_ctx.active_app()).pinned(digest)
    if resolution is None:
        raise SessionAgentUnavailable(
            str(agent_def.metadata.get("agent_ref") or agent_def.id),
            "agent_resolution_missing",
            [{"code": "agent_resolution_missing", "message": f"{digest} is not stored"}],
        )
    token = _ctx.set_session_id(session_id)
    try:
        module = build_agent_module(base_agent, agent_def, resolution)
        precede_connect_claude_code_session(
            getattr(module, "config", None),
            getattr(module, "signature", None),
            session_id=session_id,
        )
        kwargs: dict[str, Any] = {
            "question": question,
            "session_id": session_id,
            "cancel_requested": cancel_requested,
        }
        if images:
            kwargs["images"] = list(images)
        if files:
            kwargs["files"] = list(files)
        return module(**kwargs)
    finally:
        _ctx.reset(token)


__all__ = ["run_resolved_agent"]
