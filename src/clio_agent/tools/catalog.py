"""Tool ownership and visibility catalog for CLIO agents.

Core ships only the universal built-in tools (``fs``/``shell``). Their
ownership/visibility lives here as the static base catalog. Every other tool
is a *declared MCP* tool whose catalog entry is **derived at runtime** from the
connected server namespace (see ``gateway.build_tool_catalog``) plus each pack
expert's ``tools:`` list — not hand-written here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ToolCatalogEntry:
    """Static ownership and routing metadata for one CLIO tool."""

    name: str
    owner: str
    tags: frozenset[str]
    visible_to: frozenset[str]
    planner_visible: bool = True


def _entry(
    name: str,
    owner: str,
    tags: Iterable[str],
    *,
    visible_to: Iterable[str] = (),
    planner_visible: bool = True,
) -> ToolCatalogEntry:
    scopes = set(visible_to)
    scopes.add(owner)
    if planner_visible:
        scopes.add("planner")
    return ToolCatalogEntry(
        name=name,
        owner=owner,
        tags=frozenset(tags),
        visible_to=frozenset(scopes),
        planner_visible=planner_visible,
    )


# Static base catalog: the universal in-process built-ins only (fs/shell). All
# domain/case tools are declared MCPs and are derived at runtime in
# ``gateway.build_tool_catalog`` from connected namespaces + expert ``tools:``.
TOOL_CATALOG: dict[str, ToolCatalogEntry] = {
    "shell_bash": _entry(
        "shell_bash",
        "utility",
        {"utility", "shell", "local", "diagnostic"},
        visible_to={"chat"},
    ),
    "fs_propose_edit": _entry(
        "fs_propose_edit", "utility", {"workspace", "edit", "diff", "proposal"}
    ),
    "fs_read_file": _entry(
        "fs_read_file", "workspace", {"workspace", "read"}, planner_visible=False
    ),
    "fs_apply_edit_write": _entry(
        "fs_apply_edit_write", "workspace", {"workspace", "write"}, planner_visible=False
    ),
}


# The "active" catalog: the static built-ins by default, replaced at runtime by
# the derived catalog (built-ins + connected MCP namespaces + expert visibility,
# see ``gateway.build_tool_catalog``). All accessors read this so declared-MCP
# tools become visible/ownable without touching the static base dict.
_ACTIVE_CATALOG: dict[str, ToolCatalogEntry] = dict(TOOL_CATALOG)


def set_active_catalog(catalog: dict[str, ToolCatalogEntry] | None) -> None:
    """Install the runtime tool catalog the accessors consult.

    Passing ``None`` resets the active catalog to the static built-ins. The
    derived catalog from ``gateway.build_tool_catalog`` should be installed here
    once the agent's gateway is built so declared-MCP tools gain ownership and
    expert/planner visibility.
    """

    global _ACTIVE_CATALOG
    _ACTIVE_CATALOG = dict(TOOL_CATALOG) if catalog is None else dict(catalog)


def active_catalog() -> dict[str, ToolCatalogEntry]:
    """Return the currently active tool catalog."""

    return _ACTIVE_CATALOG


def get_tool_entry(tool_name: str) -> ToolCatalogEntry | None:
    """Return catalog metadata for a tool name, if CLIO owns it."""

    return _ACTIVE_CATALOG.get(tool_name)


def tool_owner(tool_name: str) -> str:
    """Return the owning expert id for a tool, or an empty string if unknown."""

    entry = get_tool_entry(tool_name)
    return entry.owner if entry else ""


def tool_tags(tool_name: str) -> frozenset[str]:
    """Return tags for a tool, or an empty set if unknown."""

    entry = get_tool_entry(tool_name)
    return entry.tags if entry else frozenset()


def tool_visible_to(tool_name: str, scope: str) -> bool:
    """Return whether a tool should be visible to an agent or planner scope."""

    entry = get_tool_entry(tool_name)
    return bool(entry and scope in entry.visible_to)


def tool_visible_scopes(tool_name: str) -> list[str]:
    """Return sorted agent/planner scopes allowed to see a tool."""

    entry = get_tool_entry(tool_name)
    return sorted(entry.visible_to) if entry else []


def tool_names_for_owner(owner: str, *, planner_visible_only: bool = True) -> list[str]:
    """Return catalog tool names owned by an expert id."""

    names = [
        entry.name
        for entry in _ACTIVE_CATALOG.values()
        if entry.owner == owner and (entry.planner_visible or not planner_visible_only)
    ]
    return sorted(names)


def filter_tool_names_for_scope(tool_names: Iterable[str], scope: str) -> list[str]:
    """Filter tool names to those visible to one agent/planner scope."""

    return sorted(name for name in tool_names if tool_visible_to(name, scope))
