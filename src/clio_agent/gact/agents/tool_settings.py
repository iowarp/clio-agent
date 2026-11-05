"""Each of an agent's tools' I/O (``callers``) and default permission (owner module).

The vocabulary and the configured values live in :mod:`clio_agent.tools.tool_settings`;
this module reads the declarations of the agent's resolved plugins and applies the
result to a built agent program, once its tools are final
(:func:`clio_agent.gact.agents.deferred_tools.apply_disclosure`).

Where a value comes from, highest first:

- ``callers``: ``tools.callers`` in the configuration; the plugin's declaration (a
  ``tool``/``native_tool`` row, else ``servers.<name>.tools.<tool>``, else
  ``servers.<name>``); the built-in's own declaration where it is constructed; ``both``.
- default permission: ``tools.permissions``; the plugin's declaration, the same way;
  else none, and the tool's annotations and the approval mode decide (the gate's
  layers, :mod:`clio_agent.gact.runtime.tool_permission_layers`).

Applying the settings to a program:

- a tool whose default permission is ``deny``, or that a person's rule names exactly
  and denies, is **blocked**: removed from the program (so from the tool list, the
  deferred catalog and the ``run_code`` stubs), with the typed reason
  ``tool_permission_deny`` in the trace; a call to it by name is refused with that
  reason. Rules with a wildcard deny at the gate instead, call by call.
- a ``code`` tool leaves the tool list and the deferred catalog; it is a kernel stub
  and a line in the ``run_code`` index, and a direct call is refused. Without
  ``run_code`` it falls back to ``direct`` with the typed reason
  ``tool_callers_code_unavailable``.
- a ``direct`` tool has no stub; a script calling it gets the typed refusal.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from clio_agent.tools.tool_settings import (
    CALLERS_ATTR,
    CALLERS_BOTH,
    CALLERS_CODE,
    CALLERS_DIRECT,
    configured_callers,
    configured_permissions,
)

__all__ = [
    "TOOL_CALLERS_CODE_UNAVAILABLE",
    "TOOL_PERMISSION_DENY",
    "Declared",
    "apply_tool_settings",
    "call_refusal",
    "declared_callers",
    "declared_permission",
    "plugin_declaration",
    "reapply_tool_settings",
]

logger = logging.getLogger(__name__)

#: Typed reason: ``code`` tools are offered directly because the agent has no ``run_code``.
TOOL_CALLERS_CODE_UNAVAILABLE = "tool_callers_code_unavailable"
#: Typed reason: a tool is blocked by its default permission or a person's rule.
TOOL_PERMISSION_DENY = "tool_permission_deny"
_RUN_CODE = "run_code"


@dataclass(frozen=True)
class Declared:
    """One setting's value and where it came from (``configuration``, ``plugin``,
    ``built-in``, ``annotations`` or ``default``)."""

    value: str
    source: str


def _server_for(name: str, servers: Mapping[str, Any]) -> tuple[str, str]:
    from clio_agent.gact.code_mode.stubs import owning_server  # noqa: PLC0415

    server = owning_server(name, servers)
    return (server, name[len(server) + 1 :]) if server else ("", "")


def plugin_declaration(resolution: Any, name: str, setting: str) -> str | None:
    """The plugin's declared ``callers`` or ``permission`` for ``name``, if any."""
    for row in getattr(resolution, "tools", []) or []:
        if row.get("name") == name:
            return row.get(setting)
    servers: Mapping[str, Mapping[str, Any]] = getattr(resolution, "mcp_servers", {}) or {}
    server, bare = _server_for(name, servers)
    if not server:
        return None
    row = servers[server] or {}
    entry = (row.get("tools") or {}).get(bare) or {}
    return entry.get(setting) or row.get(setting)


def declared_callers(name: str, tool: Any, resolution: Any) -> Declared:
    """Who may call ``name``, and from which layer."""
    configured = configured_callers().get(name)
    if configured:
        return Declared(configured, "configuration")
    declared = plugin_declaration(resolution, name, "callers")
    if declared:
        return Declared(declared, "plugin")
    own = getattr(getattr(tool, "func", None), CALLERS_ATTR, None)
    if own:
        return Declared(own, "built-in")
    return Declared(CALLERS_BOTH, "default")


def declared_permission(name: str, resolution: Any) -> Declared:
    """``name``'s default permission (``""`` when none is declared), and its layer."""
    configured = configured_permissions().get(name)
    if configured:
        return Declared(configured, "configuration")
    declared = plugin_declaration(resolution, name, "permission")
    if declared:
        return Declared(declared, "plugin")
    return Declared("", "annotations")


def _person_denies(name: str) -> bool:
    """Whether a person's rule in this session's scope denies exactly ``name``."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.runtime.grant_resolver import resolve  # noqa: PLC0415

    app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
    if app is None:
        return False
    policies = [
        row
        for row in getattr(app.state, "permission_policies", []) or []
        if isinstance(row, Mapping)
        and row.get("tool_name_pattern") == name
        and not row.get("path_pattern")
    ]
    if not policies:
        return False
    session = app.state.sessions.get(session_id) if session_id else None
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    action = resolve(
        "tool", name, policies=policies, session_id=session_id, workspace_id=workspace_id
    )
    return action == "deny"


def apply_tool_settings(program: Any, resolution: Any) -> frozenset[str]:
    """Apply the agent's tool settings to ``program``; returns its ``code`` tools.

    Sets ``_clio_blocked`` (name to typed reason), ``_clio_code_only`` and
    ``_clio_direct_only`` on the program and removes blocked tools from its tools.
    """
    from clio_agent.runtime import trace  # noqa: PLC0415

    tools: dict[str, Any] = getattr(program, "tools", {}) or {}
    _remember_facts(tools)  # before blocking: the tools page still shows a blocked tool's flags
    blocked: dict[str, str] = dict(getattr(program, "_clio_blocked", {}) or {})
    for name in list(tools):
        if declared_permission(name, resolution).value == "deny" or _person_denies(name):
            blocked[name] = TOOL_PERMISSION_DENY
            tools.pop(name, None)
    if blocked:
        trace.event("TOOLS", "%s tools=%s", TOOL_PERMISSION_DENY, ",".join(sorted(blocked)))
    callers = {name: declared_callers(name, tool, resolution).value for name, tool in tools.items()}
    code_only = frozenset(n for n, c in callers.items() if c == CALLERS_CODE)
    direct_only = frozenset(n for n, c in callers.items() if c == CALLERS_DIRECT)
    if code_only and _RUN_CODE not in tools:
        trace.event(
            "TOOLS", "%s tools=%s", TOOL_CALLERS_CODE_UNAVAILABLE, ",".join(sorted(code_only))
        )
        logger.info("%s tools=%s", TOOL_CALLERS_CODE_UNAVAILABLE, sorted(code_only))
        object.__setattr__(program, "_clio_callers_fallback", TOOL_CALLERS_CODE_UNAVAILABLE)
        code_only = frozenset()
    object.__setattr__(program, "_clio_blocked", blocked)
    object.__setattr__(program, "_clio_code_only", code_only)
    object.__setattr__(program, "_clio_direct_only", direct_only)
    object.__setattr__(program, "_clio_tool_resolution", resolution)
    return code_only


def call_refusal(program: Any, name: str) -> str:
    """Why the model may not call ``name`` directly (``""`` when it may)."""
    if name in (getattr(program, "_clio_blocked", {}) or {}):
        return (
            f"{name} is blocked by a permission setting ({TOOL_PERMISSION_DENY}); "
            "it cannot be called."
        )
    if name in (getattr(program, "_clio_code_only", frozenset()) or frozenset()):
        return (
            f"{name} can only be called from a run_code script "
            f"(await tools.<group>.<tool>(...)); see the run_code tool's index."
        )
    return ""


def reapply_tool_settings(program: Any) -> None:
    """After a live tool refresh: settle the settings of tools a server added."""
    resolution = getattr(program, "_clio_tool_resolution", None)
    if resolution is None:
        return
    code_only = apply_tool_settings(program, resolution)
    unlisted = frozenset(getattr(program, "_clio_unlisted", frozenset()))
    object.__setattr__(program, "_clio_unlisted", unlisted | code_only)


def _remember_facts(tools: Mapping[str, Any]) -> None:
    """What the tools page needs about CLIO's built-ins and plugins' Python tools, from
    the tools as built: read-only, whether it asks the gate itself, its own ``callers``."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import READ_ONLY_ATTR  # noqa: PLC0415
    from clio_agent.gact.runtime.app_state import per_app_dict  # noqa: PLC0415
    from clio_agent.tools.tool_settings import SELF_GATED_ATTR  # noqa: PLC0415

    app = _ctx.active_app()
    if app is None:
        return
    facts = per_app_dict("tool_facts", app=app)
    for name, tool in tools.items():
        func = getattr(tool, "func", None)
        if func is None or not hasattr(func, READ_ONLY_ATTR):
            continue  # MCP and gateway tools: their annotations are read where they live
        facts[name] = {
            "read_only": bool(getattr(func, READ_ONLY_ATTR, False)),
            "self_gated": bool(getattr(func, SELF_GATED_ATTR, False)),
            "callers": str(getattr(func, CALLERS_ATTR, "") or ""),
            "description": str(getattr(tool, "desc", "") or ""),
        }
