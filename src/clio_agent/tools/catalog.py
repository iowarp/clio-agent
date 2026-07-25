"""Tool ownership and visibility catalog for CLIO agents.

Core ships only the universal built-in tools (``fs``/``shell``). Their
ownership/visibility lives here as the static base catalog. Every other tool
is a *declared MCP* tool whose catalog entry is **derived at runtime** from the
connected server namespace (see ``gateway.build_tool_catalog``) plus each pack
expert's ``tools:`` list — not hand-written here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Iterable

from clio_agent.tools.servers.fs_server import FS_TOOL_ANNOTATIONS
from clio_agent.tools.servers.shell_server import SHELL_TOOL_ANNOTATIONS

#: Standard MCP boolean hint keys. A non-boolean value for any of these makes the annotation
#: block untrustworthy, so classification fails CLOSED (treated as effectful / not read-only).
_MCP_BOOLEAN_HINTS: tuple[str, ...] = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)


def _hints_well_typed(annotations: Mapping[str, Any]) -> bool:
    """Return whether every present standard boolean hint is a real ``bool``."""

    for name in _MCP_BOOLEAN_HINTS:
        value = annotations.get(name)
        if value is not None and type(value) is not bool:
            return False
    return True


def annotations_are_read_only(annotations: Any) -> bool:
    """Return whether MCP annotations explicitly and consistently declare read-only.

    MCP annotations are optional hints, so the boundary uses the one safe positive case only:
    a real boolean ``readOnlyHint=True`` with well-typed standard boolean hints and no
    contradictory ``destructiveHint=True``. Everything else (missing, malformed, contradictory)
    is NOT read-only. This is the shared classifier :mod:`clio_agent.gact.runtime.grant_resolver`
    re-exports and :func:`is_read_only` consumes, so built-ins and external MCP tools classify
    read-only through ONE predicate (#1061).
    """

    if not isinstance(annotations, Mapping):
        return False
    if not _hints_well_typed(annotations):
        return False
    return annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True


def classification_tags(annotations: Any) -> frozenset[str]:
    """Project MCP annotations to the catalog read/write classification tag(s) (#1061).

    The declared annotations are the SINGLE source of truth for a tool's effect class; the
    catalog tags are a projection of them, never hand-authored:

    * ``{"read"}``  — provably read-only (:func:`annotations_are_read_only`).
    * ``{"write"}`` — a bounded, CLOSED-world mutation (not read-only AND ``openWorldHint`` is
      EXPLICITLY ``False``): an fs write the ``auto-edits`` approval mode may auto-approve.
    * ``frozenset()`` — effectful / unclassifiable (``openWorldHint=True`` — e.g. ``shell_bash``
      — or ``openWorldHint`` absent, or absent/malformed annotations under the fail-safe default):
      NOT read-only, and NOT a catalog ``write`` either, because its effects live behind the OS
      fence (or are unbounded) rather than a positively declared bounded path set.

    FAIL-SAFE (MCP spec): ``openWorldHint`` defaults to ``True`` (open-world) when omitted, so a
    bounded ``write`` must POSITIVELY declare ``openWorldHint=False``. Absent annotations
    (``None`` / not a mapping), a malformed hint block, or a present-but-partial block that omits
    ``openWorldHint`` are all treated as most-restrictive — no ``read`` tag (so
    :func:`is_read_only` returns ``False``) and no ``write`` tag (so ``auto-edits`` does not
    auto-approve an unclassifiable or open-world tool).
    """

    if annotations_are_read_only(annotations):
        return frozenset({"read"})
    if (
        isinstance(annotations, Mapping)
        and _hints_well_typed(annotations)
        and annotations.get("openWorldHint") is False
        and (
            annotations.get("destructiveHint") is True
            or annotations.get("readOnlyHint") is False
        )
    ):
        return frozenset({"write"})
    return frozenset()


def normalize_mcp_annotations(tool: Any) -> dict[str, Any] | None:
    """Return a JSON-compatible MCP annotation mapping from a listed tool, or ``None``.

    FastMCP exposes ``annotations`` as an ``mcp.types.ToolAnnotations`` model on listed tools,
    while tests and persisted descriptor rows use plain mappings. Both normalize to a plain dict
    so the catalog projection (:func:`classification_tags`) and the permission gate's external-MCP
    context read the SAME annotations (#1061). Unknown/malformed shapes → ``None`` (fail-safe:
    missing evidence → classified effectful / requires permission).

    Single implementation shared by ``gateway._tool_annotations`` and
    ``permission_gate._normalize_mcp_tool_annotations`` (thin wrappers over this), so the
    normalization logic is not duplicated.
    """

    raw = getattr(tool, "annotations", None)
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    model_dump = getattr(raw, "model_dump", None)
    if not callable(model_dump):
        return None
    try:
        dumped = model_dump(mode="json", by_alias=True)
    except TypeError:
        dumped = model_dump()
    return dict(dumped) if isinstance(dumped, Mapping) else None


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


# Declared annotations for every built-in tool, keyed by namespaced name. The
# read/write classification tags below are PROJECTED from this mapping (the SAME
# dicts the fs/shell decorators declare), so the annotations are the single source
# of truth and no hand-authored read/write tag competes (#1061).
_BUILTIN_ANNOTATIONS: dict[str, dict[str, Any]] = {**FS_TOOL_ANNOTATIONS, **SHELL_TOOL_ANNOTATIONS}


def _builtin_entry(
    name: str,
    owner: str,
    tags: Iterable[str],
    *,
    visible_to: Iterable[str] = (),
    planner_visible: bool = True,
) -> ToolCatalogEntry:
    """Build a built-in catalog entry, PROJECTING its read/write tag from annotations.

    ``tags`` carries only the non-classification metadata (ownership/routing hints like
    ``workspace``/``shell``); the ``read``/``write`` tag is derived from the tool's declared
    MCP annotations via :func:`classification_tags`, never hand-authored here.
    """

    projected = set(tags) | classification_tags(_BUILTIN_ANNOTATIONS.get(name))
    return _entry(name, owner, projected, visible_to=visible_to, planner_visible=planner_visible)


# Static base catalog: the universal in-process built-ins only (fs/shell). All
# domain/case tools are declared MCPs and are derived at runtime in
# ``gateway.build_tool_catalog`` from connected namespaces + expert ``tools:``.
# Read/write tags are PROJECTIONS of the declared annotations (see _builtin_entry):
#   shell_bash -> effectful (openWorld) -> no read/write tag;
#   fs_propose_edit / fs_read_file -> readOnlyHint -> "read";
#   fs_apply_edit_write -> destructive, closed-world -> "write".
TOOL_CATALOG: dict[str, ToolCatalogEntry] = {
    "shell_bash": _builtin_entry(
        "shell_bash",
        "utility",
        {"utility", "shell", "local", "diagnostic"},
        visible_to={"chat"},
    ),
    "fs_propose_edit": _builtin_entry(
        "fs_propose_edit",
        "utility",
        {"workspace", "edit", "diff", "proposal"},
    ),
    "fs_read_file": _builtin_entry(
        "fs_read_file", "workspace", {"workspace"}, planner_visible=False
    ),
    "fs_apply_edit_write": _builtin_entry(
        "fs_apply_edit_write", "workspace", {"workspace"}, planner_visible=False
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
