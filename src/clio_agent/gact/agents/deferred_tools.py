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
  ``tools.defer_share`` of the model's context window and ``tools.defer_tokens``;
- a native or Python tool whose plugin says nothing, when ``tools.defer_builtins`` is
  on (the default). Across 464 evaluation sessions 54 of the 67 listed tools, 77% of
  the listing's characters, were never called (2026-11); a plugin keeps a tool listed
  by declaring ``defer=False``, and ``tools.never_defer`` (the core loop) is always
  listed.

Nothing is deferred when the agent cannot call ``search_tools`` (an allowlist without
it): the tools are listed in full and the trace records ``tools_deferral_unavailable``.
A tool loaded with ``search_tools`` joins the tool list when an applied plugin change
rebuilds the prompt after it was loaded (``metadata["loaded_tools"]``); a compaction
keeps the cached tool list, so it does not list them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from clio_agent import conf
from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog
from clio_agent.tools.mcp_server_instructions import instructions_for

__all__ = [
    "SEARCH_TOOLS",
    "TOOLS_DEFERRAL_UNAVAILABLE",
    "Disclosure",
    "apply_disclosure",
    "never_defer",
    "plan_disclosure",
    "sync_deferred_catalog",
]

SEARCH_TOOLS = "search_tools"
#: Typed trace reason: deferral was declared but the agent cannot call search_tools.
TOOLS_DEFERRAL_UNAVAILABLE = "tools_deferral_unavailable"
#: The default ``tools.never_defer``: the core loop, never deferred whatever a plugin
#: declares. An entry ending in ``*`` names a prefix.
_NEVER_DEFER: Sequence[str] = (
    SEARCH_TOOLS,
    "ask_user",
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
    "fs_*",
    "shell_*",
    # The memory injection points at these by name whenever memory is on (the only
    # time they exist); deferred, the model called memory_read blind (X2, path= for name=).
    "memory_search",
    "memory_read",
)
#: Built-in tool groups named like servers (``fs_read_file``), for ``run_code``'s stubs.
_BUILTIN_SERVERS = frozenset({"fs", "shell", "web", "literature"})


@dataclass(frozen=True)
class Disclosure:
    """An agent's deferred tools, their catalog and the prompt entry naming them."""

    deferred: frozenset[str] = frozenset()
    catalog: ToolCatalog = field(default_factory=ToolCatalog)
    entry: str = ""
    #: MCP servers deferred whole: a tool they add later is deferred too.
    deferred_servers: frozenset[str] = frozenset()


def never_defer() -> tuple[frozenset[str], tuple[str, ...]]:
    """``(names, prefixes)`` never deferred (``tools.never_defer``; ``x_*`` is a prefix)."""
    entries = conf.resolve(
        "tools.never_defer", env="CLIO_TOOLS_NEVER_DEFER", default=_NEVER_DEFER, cast=conf.as_csv
    )
    names = frozenset(entry for entry in entries if not entry.endswith("*"))
    prefixes = tuple(entry[:-1] for entry in entries if entry.endswith("*") and entry[:-1])
    return names, prefixes


#: The trace reason when ``tools.deferral`` is off and every tool is listed.
TOOLS_DEFERRAL_OFF = "tools_deferral_off"


def deferral_enabled() -> bool:
    """Whether tools may be deferred behind ``search_tools`` (``tools.deferral``, default on;
    off lists every tool, for the deferral ablation)."""
    return conf.resolve(
        "tools.deferral", env="CLIO_TOOLS_DEFERRAL", default=True, cast=conf.as_bool
    )


def defer_builtins() -> bool:
    """Whether native and Python tools that declare nothing are deferred
    (``tools.defer_builtins``, default on)."""
    return conf.resolve(
        "tools.defer_builtins", env="CLIO_TOOLS_DEFER_BUILTINS", default=True, cast=conf.as_bool
    )


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
    """The tool's MCP server (the longest matching prefix), else its plugin."""
    from clio_agent.gact.code_mode.stubs import owning_server  # noqa: PLC0415

    return owning_server(name, servers) or plugin_of.get(name, "")


def plan_disclosure(
    resolution: Any,
    tools: Mapping[str, Any],
    *,
    context_tokens: int = 0,
    loaded: frozenset[str] = frozenset(),
) -> Disclosure:
    """Plan the deferred split for an agent whose final tools are ``tools``."""
    if not deferral_enabled():
        from clio_agent.runtime import trace  # noqa: PLC0415

        trace.event("TOOLS", "%s listed=%d", TOOLS_DEFERRAL_OFF, len(tools))
        return Disclosure()
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
        by_default = defer_builtins()
        for name in names:
            declared = rows.get(name, {}).get("defer")
            if declared is True or (declared is None and by_default):
                deferred.add(name)
    never, never_prefixes = never_defer()
    deferred = {
        name
        for name in deferred - loaded - {SEARCH_TOOLS}  # the loader is never behind itself
        if name not in never and not name.startswith(never_prefixes)
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


def _instructions_summary(owner: str) -> str:
    """The listing's short form of a server's instructions, saying where the rest is.

    ``search_tools`` returns a server's full instructions with its tools
    (:func:`clio_agent.gact.search_tools.search_result`).
    """
    summary = instructions_for(owner, summary=True)
    if summary and summary != instructions_for(owner):
        summary += " (Its full instructions come with its tools from search_tools.)"
    return summary


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
        # The plugin's description of the server, else what the server says about itself
        # when it connects (MCP instructions), else its plugin's description.
        about = (
            str(server.get("description") or "")
            or _instructions_summary(owner)
            or plugin_text.get(str(server.get("plugin") or owner), "")
        )
        lines.append(f"- {owner}: {about}".rstrip(": ") if about else f"- {owner}")
        lines.append("  " + ", ".join(entry.name for entry in entries))
    return "\n".join(lines)


def apply_disclosure(
    program: Any, resolution: Any, *, context_tokens: int = 0, loaded: frozenset[str] = frozenset()
) -> str:
    """Defer the program's tools per :func:`plan_disclosure`; return the prompt entry.

    The tools' settings apply first (:mod:`.tool_settings`): blocked tools leave the
    program and ``code`` tools join ``_clio_unlisted`` without a deferred entry.
    The deferred names join ``_clio_unlisted`` (callable, left out of the tool list);
    the catalog and the deferred servers ride on the program for ``search_tools`` and
    live refresh. With nothing deferred, ``search_tools`` itself is left out of the
    list: it would find nothing.
    """
    from clio_agent.gact.agents.tool_settings import apply_tool_settings  # noqa: PLC0415
    from clio_agent.gact.code_mode.tool import finalize_run_code  # noqa: PLC0415

    code_only = apply_tool_settings(program, resolution)  # blocked tools leave here
    tools: Mapping[str, Any] = getattr(program, "tools", {}) or {}
    servers = frozenset(getattr(resolution, "mcp_servers", {}) or {}) | _BUILTIN_SERVERS
    finalize_run_code(program, servers)
    from clio_agent.plugins.kernel_bindings import attach  # noqa: PLC0415

    attach(program, resolution)  # after tool settings: a binding needs its tools script-callable
    # Code-only tools are kernel stubs: never listed, never in the deferred catalog.
    offered = {name: tool for name, tool in tools.items() if name not in code_only}
    disclosure = plan_disclosure(resolution, offered, context_tokens=context_tokens, loaded=loaded)
    unlisted = frozenset(getattr(program, "_clio_unlisted", frozenset())) | code_only
    if not disclosure.deferred and SEARCH_TOOLS in tools:
        unlisted |= {SEARCH_TOOLS}
    object.__setattr__(program, "_clio_unlisted", unlisted | disclosure.deferred)
    object.__setattr__(program, "_clio_deferred_catalog", disclosure.catalog)
    object.__setattr__(program, "_clio_deferred_servers", disclosure.deferred_servers)
    # What a live refresh compares against: a tool here already has its place (listed,
    # deferred or a stub), and the tools this session loaded stay listed (A1).
    object.__setattr__(program, "_clio_disclosed_tools", frozenset(tools))
    object.__setattr__(program, "_clio_loaded_listed", frozenset(loaded))
    object.__setattr__(program, "_clio_owner_servers", servers)
    from clio_agent.gact.mcp_app_tools import attach_app_tools  # noqa: PLC0415

    attach_app_tools(program)  # the session's view tools: deferred, owned by their server
    return disclosure.entry


def sync_deferred_catalog(program: Any) -> list[str]:
    """After a live refresh: a deferred server's new tools join the catalog, unlisted.

    Returns the names added, which the plugin-change note names to the model (TD11).

    Only a tool new since the last disclosure or sync is placed: one the agent already
    had keeps its place, so a tool loaded with ``search_tools`` (listed once the prompt
    was rebuilt) is not deferred again when its server refreshes. A new tool that
    ``tools.never_defer`` names, or that this session loaded before, stays listed.

    A tool a deferred server removed leaves the catalog; the prompt entry keeps naming
    the server's tools as they were until the prompt is rebuilt, and a search says
    which remain.
    """
    from clio_agent.gact.code_mode.stubs import owning_server  # noqa: PLC0415
    from clio_agent.gact.mcp_app_tools import APP_TOOL_PREFIX, place_app_tools  # noqa: PLC0415

    app_added = place_app_tools(program)  # a view's tools follow its list (MA12)
    deferred_servers = frozenset(getattr(program, "_clio_deferred_servers", ()) or ())
    catalog: ToolCatalog | None = getattr(program, "_clio_deferred_catalog", None)
    if not deferred_servers or catalog is None:
        return app_added
    tools: Mapping[str, Any] = getattr(program, "tools", {}) or {}
    servers = deferred_servers | frozenset(getattr(program, "_clio_owner_servers", ()) or ())
    known: frozenset[str] | None = getattr(program, "_clio_disclosed_tools", None)
    loaded = frozenset(getattr(program, "_clio_loaded_listed", frozenset()) or ())
    code_only = getattr(program, "_clio_code_only", frozenset()) or frozenset()
    never, never_prefixes = never_defer()
    added = []
    for name, tool in tools.items():
        if name in catalog or name in code_only or (known is not None and name in known):
            continue
        if name.startswith(APP_TOOL_PREFIX):
            continue
        if name in loaded or name in never or name.startswith(never_prefixes):
            continue
        owner = owning_server(name, servers)
        if owner in deferred_servers:
            catalog.add(CatalogEntry(name, owner, *_schema(tool)))
            added.append(name)
    for name in [n for n in catalog.names() if n not in tools]:
        catalog.remove(name)
    if known is not None:
        object.__setattr__(program, "_clio_disclosed_tools", frozenset(tools))
    if added:
        unlisted = frozenset(getattr(program, "_clio_unlisted", frozenset()))
        object.__setattr__(program, "_clio_unlisted", unlisted | set(added))
    return [*app_added, *added]
