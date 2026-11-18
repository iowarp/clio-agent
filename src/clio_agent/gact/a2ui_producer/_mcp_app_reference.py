"""Producer-side check of ``clio.mcp-app.v1``'s opening call (MA20).

The catalog's JSON Schema checks only the shape of ``server`` and ``tool``. It
cannot see whether that server is connected, whether it has the tool, or
whether the tool opens a view. Those facts live in the agent's tool executor,
so this module checks them when the surface is produced: a wrong name is a
typed refusal that lists the server's view tools, instead of a surface whose
app reports the failure only once someone looks at it.

A channel's ``tool`` is not checked here. It may be one of the view's own app
tools, which exist only once the view is open; the surface reports a channel
tool that neither the server nor the view has. A channel's ``key`` is checked
against the keys the opening tool declares it reports (FP-D11,
:mod:`clio_agent.gact.mcp_app_state_keys`); a tool that declares none is not checked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.a2ui_producer._refusal import refusal
from clio_agent.gact.mcp_app_state_keys import declared_state_keys, describe, state_keys
from clio_agent.gact.mcp_apps import _resource_uri

if TYPE_CHECKING:
    from fastapi import FastAPI

_MCP_APP_COMPONENT = "clio.mcp-app.v1"


def _tool_definitions(app: FastAPI) -> dict[str, Any] | None:
    agent = getattr(app.state, "agent", None)
    resolver = getattr(agent, "_active_tool_executor", None)
    executor = resolver() if callable(resolver) else getattr(agent, "tool_executor", None)
    definitions = getattr(executor, "get_all_tool_definitions", None)
    return definitions() if callable(definitions) else None


def _unknown_channel(component: dict[str, Any], definition: Any, name: str) -> str:
    """The first channel key the opening tool does not declare (``""`` when all are)."""

    declared = {item.key for item in declared_state_keys(definition, name)}
    if not declared:
        return ""
    for channel in component.get("state") or []:
        key = channel.get("key") if isinstance(channel, dict) else None
        if isinstance(key, str) and key not in declared:
            return key
    return ""


def validate_mcp_app_components(
    app: FastAPI, components: list[dict[str, Any]], *, session_id: str | None = None
) -> dict[str, Any] | None:
    """Refuse a ``clio.mcp-app.v1`` whose server has no such view-opening tool.

    Returns:
        ``None`` when every app component names a tool that opens a view, or
        when there is no app component; otherwise a typed refusal dict.
    """

    apps = [
        c for c in components if isinstance(c, dict) and c.get("component") == _MCP_APP_COMPONENT
    ]
    if not apps:
        return None
    definitions = _tool_definitions(app)
    if definitions is None:
        return refusal(
            "a2ui_mcp_app_tool_not_found",
            detail="the agent's tools are not available, so no server tool can open a view",
        )
    for component in apps:
        server = str(component.get("server") or "")
        tool = str(component.get("tool") or "")
        definition = definitions.get(f"{server}_{tool}")
        if definition is not None and _resource_uri(definition):
            unknown = _unknown_channel(component, definition, f"{server}_{tool}")
            if not unknown:
                continue
            keys = describe(state_keys(app, session_id, server, tool, definition))
            return refusal(
                "a2ui_mcp_app_state_key_unknown",
                detail=(
                    f"component {component.get('id')!r}: the {tool!r} view of {server!r} "
                    f"does not report {unknown!r}; it reports: {keys}"
                ),
            )
        view_tools = sorted(
            _with_keys(app, session_id, server, name[len(server) + 1 :], candidate)
            for name, candidate in definitions.items()
            if name.startswith(f"{server}_") and _resource_uri(candidate)
        )
        what = "does not open a view" if definition is not None else "is not a tool of that server"
        return refusal(
            "a2ui_mcp_app_tool_not_found",
            detail=(
                f"component {component.get('id')!r}: {tool!r} {what} on server {server!r}; "
                f"its tools that open a view: {', '.join(view_tools) or 'none'}"
            ),
        )
    return None


def _with_keys(app: Any, sid: str | None, server: str, tool: str, definition: Any) -> str:
    """``open_view (state keys: time (set with set_timestep))``, or the bare name."""

    keys = describe(state_keys(app, sid, server, tool, definition))
    return f"{tool} (state keys: {keys})" if keys else tool


__all__ = ["validate_mcp_app_components"]
