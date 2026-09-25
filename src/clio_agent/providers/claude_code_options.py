"""Claude Agent SDK ``ClaudeAgentOptions`` construction for the bare-model transport.

Owner module for the SDK-options glue used by the ``claude_code`` session pool
(:mod:`clio_agent.providers.claude_code_sessions` — the ONE client path, S2
B1: both the streaming and blocking entry points ride it) and the streaming
path in :mod:`clio_agent.providers.claude_code_litellm`. Kept out of those
files (#775 no-accretion) so the #895 thinking wiring and the S2 tuning pass
(B4 ``system_prompt``, B11 ``env``, B12 ``cli_path``, B15 ``max_buffer_size``)
do not regrow them.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["build_sdk_options", "require_claude_agent_sdk", "thinking_key"]


def require_claude_agent_sdk() -> Any:
    """Import the Claude Agent SDK, installing it once when absent.

    The single SDK-transport import/selection seam: every ``sdk`` path gets a
    typed reason (``ClaudeCodeCLIUnavailableError``) rather than a raw
    ``ImportError`` trace. The SDK installs via the ``claude-code`` extra; on
    the mcp-2 core its protective ``mcp<2`` bound is neutralized by the
    ``[tool.uv] override-dependencies`` entry (CLIO uses the SDK purely as an
    LLM provider and never touches its SDK-MCP-server bridging surface).
    """
    try:
        import claude_agent_sdk  # noqa: PLC0415
    except ImportError:
        from clio_agent.providers.claude_code_errors import (  # noqa: PLC0415
            CLAUDE_CODE_INSTALL_FAILED_MESSAGE,
        )
        from clio_agent.providers.claude_code_litellm import (  # noqa: PLC0415
            ClaudeCodeCLIUnavailableError,
        )
        from clio_agent.providers.dependencies import (  # noqa: PLC0415
            ProviderDependencyInstallError,
            ensure_claude_code_support,
        )

        try:
            ensure_claude_code_support()
            import claude_agent_sdk  # noqa: PLC0415
        except (ImportError, ProviderDependencyInstallError) as install_exc:
            raise ClaudeCodeCLIUnavailableError(CLAUDE_CODE_INSTALL_FAILED_MESSAGE) from install_exc
    return claude_agent_sdk


def thinking_key(thinking: dict[str, Any] | None) -> str | None:
    """Stable, hashable identity for an SDK thinking config (pool/session key)."""
    if not thinking:
        return None
    return json.dumps(thinking, sort_keys=True)


def build_sdk_options(
    *,
    model: str | None,
    cwd: str | None,
    stream: bool,
    thinking: dict[str, Any] | None,
    system_prompt: str | None = None,
    stderr: Any | None = None,
) -> Any:
    """Build ``ClaudeAgentOptions`` for the bare-model transport (both SDK paths).

    Bare-model transport: Claude Code's own tools, MCP servers, plugins, and skills
    are disabled; ``setting_sources=[]`` keeps the model isolated from filesystem
    settings; and clio's ReAct loop drives tools. ``stream`` adds
    ``include_partial_messages``. ``thinking`` (#895) is the resolved SDK thinking
    config (``{"type":"disabled"}`` / ``{"type":"enabled","budget_tokens":N}`` /
    ``{"type":"adaptive","effort":<level>}``, whose ``effort`` key becomes
    ``ClaudeAgentOptions.effort``); ``None`` sends nothing so the CLI default governs.

    Args:
        system_prompt: B4 -- CLIO's own system message as a plain string (never
            the ``claude_code`` preset). ``None``/``""`` omits the field so the
            CLI's own default (no persona injected, since ``setting_sources=[]``
            already drops the built-in Claude Code preamble) applies.
        stderr: B17 -- callback invoked with each stderr line the CLI subprocess
            writes (only piped by the SDK when this is set). ``None`` disables it.
    """
    from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415

    from clio_agent.providers.claude_code_runtime import (  # noqa: PLC0415
        MAX_BUFFER_SIZE,
        QUIET_ENV,
        resolve_cli_path,
    )

    kwargs: dict[str, Any] = {
        "tools": [],
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
        # B11: quiet environment (telemetry/update-check network calls skipped at
        # CLI startup). Merged by the SDK with the inherited process env.
        "env": dict(QUIET_ENV),
        # B12: pinned bundled CLI (``None`` restores the SDK's own discovery when
        # this platform's wheel does not vendor one).
        "cli_path": resolve_cli_path(),
        # B15: named constant, not the SDK's tight 1 MiB default -- see
        # claude_code_runtime.MAX_BUFFER_SIZE for why.
        "max_buffer_size": MAX_BUFFER_SIZE,
    }
    if model:
        kwargs["model"] = model
    if stream:
        kwargs["include_partial_messages"] = True
    if system_prompt:
        kwargs["system_prompt"] = system_prompt
    if stderr is not None:
        kwargs["stderr"] = stderr
    if thinking is not None:
        # resolve_thinking carries the SDK effort inside the thinking config so
        # every path (and the session-pool key) sees one value; the SDK takes it
        # as its own option (CLI ``--effort``).
        config = dict(thinking)
        effort = config.pop("effort", None)
        kwargs["thinking"] = config
        if effort is not None:
            kwargs["effort"] = effort
    return ClaudeAgentOptions(**kwargs)
