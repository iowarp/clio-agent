"""The agent part of the system prompt, as the agent's final tools allow it.

:func:`final_agent_prompt` is called by the agent builder once the agent's tools are
final (instrumented, narrowed by session posture and allowlists), so the prompt keeps
only the fragments those tools need (:mod:`clio_agent.plugins.compose_final`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from clio_agent.plugins.compose_final import final_prompt_text

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef
    from clio_agent.plugins.resolve import Resolution

__all__ = ["final_agent_prompt"]


def final_agent_prompt(
    agent_def: "AgentDef", resolution: "Resolution", tools: Iterable[Any]
) -> str:
    """``agent_def``'s prompt with only the fragments its final ``tools`` need."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import session_is_delegated  # noqa: PLC0415

    app, session_id = _ctx.active_app(), _ctx.active_session_id()
    delegated = bool(app is not None and session_id and session_is_delegated(app, session_id))
    names = {str(getattr(tool, "name", tool)) for tool in tools}
    return final_prompt_text(
        agent_def.system_prompt, resolution.prompt, names, delegated=delegated
    ).strip()
