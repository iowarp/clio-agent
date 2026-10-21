"""Per-session runtime context appended to the composed main prompt.

The composed prompt itself comes from the agent's plugins
(:mod:`clio_agent.plugins.compose`). What this module adds is runtime state the
plugins cannot know: the session's bound workspace and its write roots. It is
labelled as runtime context in the prompt view, not attributed to a plugin.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI


def _runtime_active_workspace_context(app: "FastAPI", *, session_id: str = "") -> str:
    """Surface the session's workspace scope to the agent.

    Workspace facts tell the agent where generated artifacts belong. They change
    no permissions and force no paths: the file policy validates access.
    """

    if app is None:
        return ""
    from clio_agent.gact.agents import resolution as _resolution  # noqa: PLC0415

    root = _resolution._runtime_workspace_catalog_cwd(app, session_id=session_id)
    if root is None:
        return ""
    try:
        from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

        allowed_roots = [str(item) for item in FileAccessPolicy.from_env().allowed_roots]
    except Exception as exc:  # noqa: BLE001 - policy env read; degrade to no roots line
        trace.event("WORKSPACE-CTX", "allowed-roots read failed (%s); omitting roots line", exc)
        allowed_roots = []
    lines = [
        f"Active workspace root: {root}",
        (
            "Write generated artifacts (files, charts, exports) inside the "
            "active workspace root using absolute paths so they stay with the "
            "session. Read inputs from wherever the user points you."
        ),
    ]
    if allowed_roots:
        lines.append("Writes are only permitted under: " + ", ".join(allowed_roots) + ".")
    return "\n".join(lines)


def _provider_summary(provider: Any) -> str:
    if provider is None:
        return "{}"
    # ``app.state.lm_config`` is a plain dict (set by ``PUT /v1/providers/lm``);
    # a genuinely unserializable input is stringified with a trace reason.
    try:
        if is_dataclass(provider) and not isinstance(provider, type):
            payload: Any = asdict(provider)
        elif isinstance(provider, Mapping):
            payload = dict(provider)
        else:
            raise TypeError(
                f"provider summary is neither a dataclass nor a mapping: {type(provider).__name__}"
            )
        return json.dumps(payload, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 - genuinely bad input; stringify
        trace.event("PROMPT-CTX", "provider summary serialize failed (%s); using repr", exc)
        return str(provider)


def _prompt_render_context(app: "FastAPI") -> dict[str, str]:
    """The CLIO-owned dynamic context exposed to prompt templates.

    Session-scoped keys (the session's agent, its subagents and the commands
    it may invoke) are added by the request-scoped builder in ``gact.app``.
    """

    from clio_agent.gact.catalog import _builtin_tools  # noqa: PLC0415

    tools = [f"- {tool.id}: {tool.description}" for tool in _builtin_tools()]
    return {
        "tools.available": "\n".join(tools) or "(no declared tools)",
        "memory.policy_summary": "Session-local by default; same-workspace/global reads require explicit policy or user intent.",
        "permissions.policy_summary": "Permission-controlled actions must use CLIO policy gates and visible provenance.",
        "provider.current": _provider_summary(getattr(app.state, "lm_config", None)),
    }
