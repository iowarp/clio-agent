"""Claude Agent SDK ``ClaudeAgentOptions`` construction for the bare-model transport.

Owner module for the SDK-options glue used by both claude_code SDK paths (the
persistent ``_SdkSession`` pool and the streaming path in
:mod:`clio_agent.providers.claude_code_litellm`). Kept out of that god file (#775
no-accretion) so the #895 thinking wiring does not regrow it.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["build_sdk_options", "require_claude_agent_sdk", "thinking_key"]


def require_claude_agent_sdk() -> Any:
    """Import the Claude Agent SDK or raise a typed unavailability error (#1107).

    The single SDK-transport import/selection seam: every ``sdk`` path gets a
    typed reason (``ClaudeCodeCLIUnavailableError``) rather than a raw
    ``ImportError`` trace. The SDK installs via the ``claude-code`` extra; on
    the mcp-2 core its protective ``mcp<2`` bound is neutralized by the
    ``[tool.uv] override-dependencies`` entry (CLIO uses the SDK purely as an
    LLM provider and never touches its SDK-MCP-server bridging surface).
    """
    try:
        import claude_agent_sdk  # noqa: PLC0415
    except ImportError as exc:
        from clio_agent.providers.claude_code_litellm import (  # noqa: PLC0415
            ClaudeCodeCLIUnavailableError,
        )

        raise ClaudeCodeCLIUnavailableError(
            "Claude Agent SDK transport (claude_code_transport='sdk') requires the "
            "claude-agent-sdk package. Install the 'claude-code' extra "
            "(uv sync --extra claude-code)."
        ) from exc
    return claude_agent_sdk


def thinking_key(thinking: dict[str, Any] | None) -> str | None:
    """Stable, hashable identity for an SDK thinking config (pool/session key)."""
    if not thinking:
        return None
    return json.dumps(thinking, sort_keys=True)


def build_sdk_options(
    *, model: str, cwd: str | None, stream: bool, thinking: dict[str, Any] | None
) -> Any:
    """Build ``ClaudeAgentOptions`` for the bare-model transport (both SDK paths).

    Bare-model transport: Claude Code's own tools, MCP servers, plugins, and skills
    are disabled; ``setting_sources=[]`` keeps the model isolated from filesystem
    settings; and clio's ReAct loop drives tools. ``stream`` adds
    ``include_partial_messages``. ``thinking`` (#895) is the resolved SDK thinking
    config (``{"type":"disabled"}`` / ``{"type":"enabled","budget_tokens":N}``);
    ``None`` sends nothing so the provider/CLI default governs.
    """
    from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415

    kwargs: dict[str, Any] = {
        "tools": [],
        "model": model,
        # ``max_turns`` counts assistant turns per SDK SESSION, not per query. A
        # #901 delta run intentionally sends many queries under ONE session_id, so
        # 1 kills the run's second delta query with error_max_turns (surfaced by
        # AGENT-COPPER14 turn 2 once scope-keyed connections made delta engage
        # reliably). 0 = unlimited: clio removed deterministic turn caps from its
        # agent — the model decides when it is done, and with ``tools=[]`` the SDK
        # cannot agent-loop within a query anyway.
        "max_turns": 0,
        "allowed_tools": [],
        # An empty mcp_servers map alone does not suppress MCPs from user, project,
        # or plugin configuration. The SDK documents strict_mcp_config as the
        # isolation switch; keep both explicit so account-local connectors never
        # appear in CLIO's bare-model prompt.
        "mcp_servers": {},
        "strict_mcp_config": True,
        # ``None`` means the CLI's default skill discovery still applies. An empty
        # list is the SDK's explicit "skills off" value.
        "skills": [],
        "plugins": [],
        "permission_mode": "bypassPermissions",
        "setting_sources": [],
        "cwd": cwd,
    }
    if stream:
        kwargs["include_partial_messages"] = True
    if thinking is not None:
        kwargs["thinking"] = thinking
    return ClaudeAgentOptions(**kwargs)
