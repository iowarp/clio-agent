"""Which of an agent's tools are deferred, and the prompt entry that names them.

A deferred tool stays callable (it is in the agent's tools, under ``_clio_unlisted``)
but its schema is left out of the tool list sent each step. The composed prompt
carries one entry per deferred owner (MCP server or plugin): its description and the
names of its deferred tools, so the agent always knows the tool exists, and
``search_tools`` returns the schemas (:mod:`clio_agent.gact.search_tools`).

What is deferred:

- a tool or server whose plugin declares ``defer`` (``native_tool(name, defer=True)``,
  ``tool(defer=True)``, or ``defer`` / ``servers.<name>.defer`` in the plugin's
  ``ai.iowarp.clio`` extension);
- an MCP server whose plugin says nothing, when its schemas exceed the smaller of
  ``tools.defer_share`` of the model's context window and ``tools.defer_tokens``.
  A plugin's own native and Python tools are deferred only when it says so.

Nothing is deferred when the agent cannot call ``search_tools`` (an allowlist without
it): the tools are listed in full and the trace records ``tools_deferral_unavailable``.
A tool loaded with ``search_tools`` joins the tool list once a compaction has re-based
the prompt after it was loaded (``metadata["loaded_tools"]``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from clio_agent import conf
from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog

__all__ = [
    "SEARCH_TOOLS",
    "TOOLS_DEFERRAL_UNAVAILABLE",
    "Disclosure",
    "apply_disclosure",
    "plan_disclosure",
    "sync_deferred_catalog",
]

SEARCH_TOOLS = "search_tools"
#: Typed trace reason: deferral was declared but the agent cannot call search_tools.
TOOLS_DEFERRAL_UNAVAILABLE = "tools_deferral_unavailable"
#: The core loop: never deferred whatever a plugin declares.
_NEVER = frozenset(
    {
        SEARCH_TOOLS,
        "load_skill",
        "submit",
        "run_code",
        "wait_tasks",
        "spawn_agent_task",
        "spawn_agents_parallel",
        "write_todos",
        "plan_enter",
        "plan_update",
        "plan_exit",
    }
)
_NEVER_PREFIXES = ("fs_", "shell_")
#: Built-in tool groups named like servers (``fs_read_file``), for ``run_code``'s stubs.
_BUILTIN_SERVERS = frozenset({"fs", "shell", "web"})


@dataclass(frozen=True)
class Disclosure:
    """An agent's deferred tools, their catalog and the prompt entry naming them."""

    deferred: frozenset[str] = frozenset()
    catalog: ToolCatalog = field(default_factory=ToolCatalog)
    entry: str = ""
    #: MCP servers deferred whole: a tool they add later is deferred too.
    deferred_servers: frozenset[str] = frozenset()


def _share() -> float:
    return conf.resolve("tools.defer_share", env="CLIO_TOOLS_DEFER_SHARE", default=0.05, cast=float)


def _cap_tokens() -> int:
    return conf.resolve(
        "tools.defer_tokens", env="CLIO_TOOLS_DEFER_TOKENS", default=4000, cast=conf.as_int
    )


def _schema(tool: Any) -> tuple[str, Mapping[str, Any]]:
    from clio_agent.gact.agents.clio_react import _function_tool  # noqa: PLC0415

    spec = _function_tool(tool)
    return str(spec.description or ""), dict(spec.parameters or {})


def _owner(name: str, servers: Mapping[str, Any], plugin_of: Mapping[str, str]) -> str:
    for server in servers:
        if name.startswith(f"{server}_"):
            return server
    return plugin_of.get(name, "")


def plan_disclosure(
    resolution: Any,
    tools: Mapping[str, Any],
    *,
    context_tokens: int = 0,
    loaded: frozenset[str] = frozenset(),
) -> Disclosure:
    """Plan the deferred split for an agent whose final tools are ``tools``."""
    servers: Mapping[str, Mapping[str, Any]] = getattr(resolution, "mcp_servers", {}) or {}
    rows = {row["name"]: row for row in getattr(resolution, "tools", []) or []}
    plugin_of = {name: str(row.get("plugin") or "") for name, row in rows.items()}
    threshold = _cap_tokens()
    if context_tokens > 0:
        threshold = min(threshold, int(context_tokens * _share()))
    by_owner: dict[str, list[str]] = {}
    for name in tools:
        by_owner.setdefault(_owner(name, servers, plugin_of), []).append(name)
    deferred: set[str] = set()
    deferred_servers: set[str] = set()
    for owner, names in by_owner.items():
        server = servers.get(owner)
        if server is not None:
            declared = server.get("defer")
            if declared is None:
                size = sum(len(json.dumps(_schema(tools[n]), default=str)) for n in names) // 4
                declared = size > threshold
            if declared:
                deferred.update(names)
                deferred_servers.add(owner)
            continue
        deferred.update(name for name in names if rows.get(name, {}).get("defer") is True)
    deferred = {
        name
        for name in deferred - loaded
        if name not in _NEVER and not name.startswith(_NEVER_PREFIXES)
    }
    if not deferred:
        return Disclosure()
    if SEARCH_TOOLS not in tools:
        from clio_agent.runtime import trace  # noqa: PLC0415

        trace.event("TOOLS", "%s deferred=%d", TOOLS_DEFERRAL_UNAVAILABLE, len(deferred))
        return Disclosure()
    catalog = ToolCatalog(
        CatalogEntry(name, _owner(name, servers, plugin_of), *_schema(tools[name]))
        for name in deferred
    )
    plugin_text = {
        str(row.get("id") or ""): str(row.get("description") or "")
        for row in getattr(resolution, "plugins", []) or []
    }
    return Disclosure(
        frozenset(deferred),
        catalog,
        _entry(catalog, servers, plugin_text),
        frozenset(deferred_servers),
    )


def _entry(
    catalog: ToolCatalog, servers: Mapping[str, Mapping[str, Any]], plugin_text: Mapping[str, str]
) -> str:
    lines = [
        "## Tools you load when needed",
        "",
        "These tools are available but not in your tool list. Load the ones you need with "
        "search_tools, by exact names (`select:name_a,name_b`) or by keywords; a loaded tool "
        "can be called from your next step.",
        "",
    ]
    for owner, entries in catalog.by_owner().items():
        server = servers.get(owner) or {}
        # A server's own description, else its plugin's (a plugin with one server).
        about = str(server.get("description") or "") or plugin_text.get(
            str(server.get("plugin") or owner), ""
        )
        lines.append(f"- {owner}: {about}".rstrip(": ") if about else f"- {owner}")
        lines.append("  " + ", ".join(entry.name for entry in entries))
    return "\n".join(lines)


def apply_disclosure(
    program: Any, resolution: Any, *, context_tokens: int = 0, loaded: frozenset[str] = frozenset()
) -> str:
    """Defer the program's tools per :func:`plan_disclosure`; return the prompt entry.

    The deferred names join ``_clio_unlisted`` (callable, left out of the tool list);
    the catalog and the deferred servers ride on the program for ``search_tools`` and
    live refresh. With nothing deferred, ``search_tools`` itself is left out of the
    list: it would find nothing.
    """
    from clio_agent.gact.code_mode.tool import finalize_run_code  # noqa: PLC0415

    tools: Mapping[str, Any] = getattr(program, "tools", {}) or {}
    servers = frozenset(getattr(resolution, "mcp_servers", {}) or {}) | _BUILTIN_SERVERS
    finalize_run_code(program, servers)
    disclosure = plan_disclosure(resolution, tools, context_tokens=context_tokens, loaded=loaded)
    unlisted = frozenset(getattr(program, "_clio_unlisted", frozenset()))
    if not disclosure.deferred and SEARCH_TOOLS in tools:
        unlisted |= {SEARCH_TOOLS}
    object.__setattr__(program, "_clio_unlisted", unlisted | disclosure.deferred)
    object.__setattr__(program, "_clio_deferred_catalog", disclosure.catalog)
    object.__setattr__(program, "_clio_deferred_servers", disclosure.deferred_servers)
    return disclosure.entry


def sync_deferred_catalog(program: Any) -> None:
    """After a live refresh: a deferred server's new tools join the catalog, unlisted.

    A tool a deferred server removed leaves the catalog; the prompt entry keeps naming
    the server's tools as they were until the prompt is rebuilt, and a search says
    which remain.
    """
    servers = tuple(f"{s}_" for s in getattr(program, "_clio_deferred_servers", ()) or ())
    catalog: ToolCatalog | None = getattr(program, "_clio_deferred_catalog", None)
    if not servers or catalog is None:
        return
    tools: Mapping[str, Any] = getattr(program, "tools", {}) or {}
    added = []
    for name, tool in tools.items():
        if name.startswith(servers) and name not in catalog:
            owner = next(s[:-1] for s in servers if name.startswith(s))
            catalog.add(CatalogEntry(name, owner, *_schema(tool)))
            added.append(name)
    for name in [n for n in catalog.names() if n not in tools]:
        catalog.remove(name)
    if added:
        unlisted = frozenset(getattr(program, "_clio_unlisted", frozenset()))
        object.__setattr__(program, "_clio_unlisted", unlisted | set(added))
