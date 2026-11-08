"""Copilot SDK client and session options: a model transport, nothing else.

Every Copilot agent feature that could act on the machine or read ambient
state is switched off explicitly, so CLIO's gateway stays the only tool path:
no built-in, custom or MCP tools, no skills, memory, file hooks, custom
instructions, config discovery, git operations, session store, autopilot
continuation or infinite-session compaction. The client runs in ``empty``
mode under a CLIO-owned ``COPILOT_HOME`` and never uses the runtime's stored
login (``use_logged_in_user=False``). ``tests/test_providers/test_copilot_options.py``
pins every flag here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from clio_agent import paths
from clio_agent.providers.copilot.runtime import runtime_env

#: Fed back to the runtime if it ever asks to run something despite no tools.
PERMISSION_DENIED_FEEDBACK = (
    "CLIO runs every tool through its own gateway; Copilot tools are disabled."
)


def deny_permission(request: Any, invocation: Any) -> Any:
    """Refuse every runtime permission request (there is no tool CLIO lets it run)."""
    from copilot.generated.rpc import PermissionDecisionReject  # noqa: PLC0415

    return PermissionDecisionReject(feedback=PERMISSION_DENIED_FEEDBACK)


def copilot_home() -> Path:
    """The CLIO-owned ``COPILOT_HOME``: the runtime never reads the user's ``~/.copilot``."""
    home = paths.user_data_dir() / "copilot" / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def client_kwargs(
    *, github_token: str, runtime_path: str, working_directory: str
) -> dict[str, Any]:
    """``CopilotClient(...)`` keyword arguments for one GACT session."""
    from copilot import RuntimeConnection  # noqa: PLC0415

    return {
        "connection": RuntimeConnection.for_stdio(path=runtime_path),
        "env": runtime_env(),
        "github_token": github_token,
        "use_logged_in_user": False,
        "base_directory": str(copilot_home()),
        "mode": "empty",
        "log_level": "error",
        "working_directory": working_directory,
    }


def session_kwargs(
    *,
    model: str,
    system_prompt: str,
    working_directory: str,
    on_event: Callable[[Any], None],
    github_token_provider: Callable[..., Any],
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """``client.create_session(...)`` keyword arguments for one Copilot session."""
    kwargs: dict[str, Any] = {
        "on_permission_request": deny_permission,
        "model": model,
        "system_message": {"mode": "replace", "content": system_prompt},
        "available_tools": [],
        "tools": [],
        "mcp_servers": {},
        "working_directory": working_directory,
        "streaming": True,
        "enable_session_telemetry": False,
        "autopilot": {"continuation_enabled": False},
        "skip_custom_instructions": True,
        "enable_config_discovery": False,
        "enable_on_demand_instruction_discovery": False,
        "enable_file_hooks": False,
        "enable_host_git_operations": False,
        "enable_session_store": False,
        "enable_skills": False,
        "skip_embedding_retrieval": True,
        "infinite_sessions": {"enabled": False},
        "memory": {"enabled": False},
        "on_event": on_event,
        "github_token_provider": github_token_provider,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


__all__ = [
    "PERMISSION_DENIED_FEEDBACK",
    "client_kwargs",
    "copilot_home",
    "deny_permission",
    "session_kwargs",
]
