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

    Bare-model transport: Claude Code's own tools are disabled, ``max_turns=1``,
    ``setting_sources=[]`` (the model sees only clio's transcript, never
    ``~/.claude`` settings), and clio's ReAct loop drives tools. ``stream`` adds
    ``include_partial_messages``. ``thinking`` (#895) is the resolved SDK thinking
    config (``{"type":"disabled"}`` / ``{"type":"enabled","budget_tokens":N}``);
    ``None`` sends nothing so the provider/CLI default governs.
    """
    from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415

    kwargs: dict[str, Any] = {
        "tools": [],
        "model": model,
        "max_turns": 1,
        "allowed_tools": [],
        "permission_mode": "bypassPermissions",
        "setting_sources": [],
        "cwd": cwd,
    }
    if stream:
        kwargs["include_partial_messages"] = True
    if thinking is not None:
        kwargs["thinking"] = thinking
    return ClaudeAgentOptions(**kwargs)
