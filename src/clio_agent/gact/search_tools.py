"""``search_tools``: load the schemas of tools the prompt names but does not list.

A deferred tool (:mod:`clio_agent.gact.agents.deferred_tools`) is callable, but its
schema is not in the tool list the model gets each step. The system prompt names it
under "Tools you load when needed"; ``search_tools`` returns its description and JSON
schema in the observation, after which the agent calls it like any other tool.

The query forms are :meth:`clio_agent.gact.tool_catalog.ToolCatalog.search`'s:
``select:a,b`` for exact names, ``+term rest`` to require ``term`` in the name, or
keywords. Any word that is exactly a tool's name is returned first.

Loaded names are recorded on the session (``metadata["loaded_tools"]``): ``pending``
until the prompt is next rebuilt (a compaction or an applied plugin change), then
``listed``, and a listed tool is built into the tool list from then on rather than
deferred, so a tool the agent depends on stops costing a search after the rebuild.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog
from clio_agent.runtime import trace

__all__ = [
    "LOADED_TOOLS_KEY",
    "build_search_tools_tool",
    "listed_loaded_tools",
    "promote_loaded_tools",
    "search_result",
]

LOADED_TOOLS_KEY = "loaded_tools"
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20
#: How many deferred names a miss lists (the prompt entry names them all).
_MISS_NAMES = 40


def _metadata(app: Any, session_id: str) -> Mapping[str, Any]:
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    metadata = getattr(session, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def _loaded(app: Any, session_id: str) -> dict[str, list[str]]:
    row = _metadata(app, session_id).get(LOADED_TOOLS_KEY)
    row = row if isinstance(row, Mapping) else {}
    return {key: [str(n) for n in row.get(key) or ()] for key in ("pending", "listed")}


def listed_loaded_tools(app: Any, session_id: str) -> frozenset[str]:
    """Tools this session loaded before its prompt was last rebuilt (listed, not deferred)."""
    return frozenset(_loaded(app, session_id)["listed"])


def _record_loaded(app: Any, session_id: str, names: Iterable[str]) -> None:
    if app is None or not session_id:
        return
    row = _loaded(app, session_id)
    new = [n for n in names if n not in row["pending"] and n not in row["listed"]]
    if new:
        row["pending"] += new
        app.state.sessions.update(session_id, metadata_patch={LOADED_TOOLS_KEY: row})


def promote_loaded_tools(app: Any, session_id: str) -> None:
    """The prompt is being rebuilt: tools loaded so far join the tool list."""
    row = _loaded(app, session_id)
    if row["pending"]:
        row = {"pending": [], "listed": [*row["listed"], *row["pending"]]}
        app.state.sessions.update(session_id, metadata_patch={LOADED_TOOLS_KEY: row})


def _signature(entry: CatalogEntry) -> str:
    properties = entry.parameters.get("properties") if entry.parameters else None
    required = set(entry.parameters.get("required") or ()) if entry.parameters else set()
    args = []
    for name, spec in (properties or {}).items():
        kind = spec.get("type", "any") if isinstance(spec, Mapping) else "any"
        args.append(f"{name}{'' if name in required else '?'}: {kind}")
    return f"{entry.name}({', '.join(args)})"


def search_result(
    catalog: ToolCatalog | None, query: str, limit: int = _DEFAULT_LIMIT
) -> tuple[str, dict[str, Any]]:
    """The model's text and the structured result of one search."""
    limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    if catalog is None or not len(catalog):
        text = "Nothing is deferred for this agent: every tool it has is already in its tool list."
        return text, {"query": query, "tools": [], "missing": [], "deferred_total": 0}
    found, missing = catalog.search(query, limit)
    structured: dict[str, Any] = {
        "query": query,
        "tools": [
            {
                "name": entry.name,
                "owner": entry.owner,
                "description": entry.description,
                "signature": _signature(entry),
            }
            for entry in found
        ],
        "missing": missing,
        "deferred_total": len(catalog),
    }
    lines: list[str] = []
    if found:
        lines.append(
            f"Loaded {len(found)} tool{'s' if len(found) != 1 else ''}; "
            "call them from your next step with the arguments their schemas give."
        )
        for entry in found:
            schema = json.dumps(entry.parameters, separators=(",", ":"), default=str)
            lines += ["", f"## {entry.name}  ({entry.owner})", entry.description.strip()]
            lines.append(f"Arguments (JSON schema): {schema}")
    if missing:
        lines += ["", f"No deferred tool is named {', '.join(missing)}."]
    if not found:
        names = catalog.names()
        shown = ", ".join(names[:_MISS_NAMES])
        more = f" and {len(names) - _MISS_NAMES} more" if len(names) > _MISS_NAMES else ""
        lines.append(
            f'No deferred tool matches "{query}". The deferred tools are: {shown}{more}. '
            "Search again by exact name (select:name) or other keywords."
        )
    return "\n".join(lines).strip(), structured


def _present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    del result
    row = structured if isinstance(structured, Mapping) else {}
    tools = [t for t in row.get("tools") or [] if isinstance(t, Mapping)]
    query = str(row.get("query") or args.get("query") or "")
    blocks: list[dict[str, Any]] = [
        {"id": "query", "type": "text", "label": "Search", "text": query}
    ]
    for i, tool in enumerate(tools):
        text = "\n".join(
            part
            for part in (str(tool.get("signature") or ""), str(tool.get("description") or ""))
            if part
        )
        blocks.append(
            {
                "id": f"tool-{i}",
                "type": "text",
                "label": f"{tool.get('name')} ({tool.get('owner') or 'tool'})",
                "text": text,
            }
        )
    missing = [str(name) for name in row.get("missing") or []]
    if missing:
        blocks.append(
            {
                "id": "missing",
                "type": "text",
                "severity": "warning",
                "text": f"Not found: {', '.join(missing)}",
            }
        )
    if tools:
        summary = f"Loaded {len(tools)} tool{'s' if len(tools) != 1 else ''}"
        status = ""
    elif row.get("deferred_total") == 0:
        summary, status = "No tools are deferred", ""
    else:
        summary, status = "No matching tools", "degraded"
        blocks.append(
            {
                "id": "no-match",
                "type": "text",
                "severity": "warning",
                "text": f"None of the {row.get('deferred_total', 0)} deferred tools matched.",
            }
        )
    view: dict[str, Any] = {"subject": "query", "summary": summary, "blocks": blocks}
    return view | ({"status": status} if status else {})


def _starting(args: Mapping[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "")
    return {
        "subject": "query",
        "summary": "",
        "blocks": [{"id": "query", "type": "text", "label": "Search", "text": query}],
    }


def build_search_tools_tool() -> Any:
    """The ``search_tools`` native tool."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
        native_tool,
    )

    def search_tools(query: str, limit: int = _DEFAULT_LIMIT) -> str:
        """Load the schemas of deferred tools by exact name or keywords."""
        loop = active_loop()
        catalog = getattr(getattr(loop, "agent", None), "_clio_deferred_catalog", None)
        text, structured = search_result(catalog, query, limit)
        names = [row["name"] for row in structured["tools"]]
        _record_loaded(_ctx.active_app(), _ctx.active_session_id(), names)
        trace.event(
            "TOOLS",
            "search_tools query=%r loaded=%s missing=%s",
            query,
            names,
            structured["missing"],
        )
        declare_structured_content(structured)
        return text

    # Read-only: it reveals schemas and changes nothing the agent acts on; the session
    # only notes which tools were loaded, so the next prompt rebuild can list them.
    return native_tool(
        search_tools,
        name="search_tools",
        read_only=True,
        presentation=_present,
        presentation_start=_starting,
        domain="agents",
        title="Load tools",
        desc=(
            "Load tools named under 'Tools you load when needed' so you can call them. "
            "Query `select:name_a,name_b` for exact names, or keywords describing what you "
            "need (`+word more words` requires `word` in the tool name). The result gives "
            "each tool's description and argument schema; call it from your next step. "
            "Load every tool you expect to need in one search."
        ),
        args={
            "query": {
                "type": "string",
                "description": "select:name_a,name_b, or keywords for what you need.",
            },
            "limit": {
                "type": "integer",
                "description": f"Most tools to return for a keyword search (default "
                f"{_DEFAULT_LIMIT}, at most {_MAX_LIMIT}).",
            },
        },
    )
