"""``search_tools``: load the schemas of tools the prompt names but does not list.

A deferred tool (:mod:`clio_agent.gact.agents.deferred_tools`) is callable, but its
schema is not in the tool list the model gets each step. The system prompt names it
under "Tools you load when needed"; ``search_tools`` returns its description and JSON
schema in the observation, after which the agent calls it like any other tool.

The query forms are :meth:`clio_agent.gact.tool_catalog.ToolCatalog.search`'s:
``select:a,b`` for exact names, ``+term rest`` to require ``term`` in the name, or
keywords. Any word that is exactly a tool's name is returned first.

Loaded names are recorded on the session (``metadata["loaded_tools"]``): ``pending``
until the prompt is next rebuilt by an applied plugin change, then ``listed``, and a
listed tool is built into the tool list from then on rather than deferred. A
compaction does not list them: it keeps the cached system prompt and tool list, and a
change there would cost a whole-prompt cache write at the next turn.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from clio_agent import conf
from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog
from clio_agent.runtime import trace

__all__ = [
    "LOADED_TOOLS_KEY",
    "build_search_tools_tool",
    "listed_loaded_tools",
    "promote_loaded_tools",
    "search_limit",
    "search_max",
    "search_miss_names",
    "search_result",
]

LOADED_TOOLS_KEY = "loaded_tools"


def _at_least(key: str, env: str, default: int, floor: int) -> int:
    value = conf.resolve(key, env=env, default=default, cast=conf.as_int)
    if value < floor:
        raise ValueError(f"{key} must be at least {floor}, not {value}")
    return value


def search_limit() -> int:
    """Tools a keyword search returns when the call names no limit."""
    return _at_least("tools.search_limit", "CLIO_TOOLS_SEARCH_LIMIT", 5, 1)


def search_max() -> int:
    """The most tools one keyword search returns, whatever limit the call names."""
    return _at_least("tools.search_max", "CLIO_TOOLS_SEARCH_MAX", 20, 1)


def search_miss_names() -> int:
    """How many deferred names a miss lists (the prompt entry names them all)."""
    return _at_least("tools.search_miss_names", "CLIO_TOOLS_SEARCH_MISS_NAMES", 40, 0)


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


def _server_instructions(found: Iterable[CatalogEntry]) -> list[dict[str, str]]:
    """The whole instructions of each server whose tools a search loads, once each.

    The prompt's deferred listing carries only a short form; loading a server's tools
    is when its own guidance for them matters.
    """
    from clio_agent.tools.mcp_server_instructions import instructions_for  # noqa: PLC0415

    rows: list[dict[str, str]] = []
    for owner in dict.fromkeys(entry.owner for entry in found):
        if text := instructions_for(owner):
            rows.append({"name": owner, "instructions": text})
    return rows


def search_result(
    catalog: ToolCatalog | None, query: str, limit: int = 0
) -> tuple[str, dict[str, Any]]:
    """The model's text and the structured result of one search (``limit`` 0: the default)."""
    limit = max(1, min(int(limit or search_limit()), search_max()))
    if catalog is None or not len(catalog):
        text = "Nothing is deferred for this agent: every tool it has is already in its tool list."
        return text, {"query": query, "tools": [], "missing": [], "deferred_total": 0}
    found, missing = catalog.search(query, limit)
    structured: dict[str, Any] = {
        "query": query,
        "tools": [
            {
                "name": entry.name,
                "title": _title(entry.name),
                "owner": entry.owner,
                "description": entry.description,
                "signature": _signature(entry),
            }
            for entry in found
        ],
        "missing": missing,
        "deferred_total": len(catalog),
        "servers": _server_instructions(found),
    }
    lines: list[str] = []
    if found:
        lines.append(
            f"Loaded {len(found)} tool{'s' if len(found) != 1 else ''}; "
            "call them from your next step with the arguments their schemas give."
        )
        for server in structured["servers"]:
            lines += ["", f"## {server['name']} server instructions", server["instructions"]]
        for entry in found:
            schema = json.dumps(entry.parameters, separators=(",", ":"), default=str)
            lines += ["", f"## {entry.name}  ({entry.owner})", entry.description.strip()]
            lines.append(f"Arguments (JSON schema): {schema}")
    if missing:
        lines += ["", f"No deferred tool is named {', '.join(missing)}."]
    if not found:
        names = catalog.names()
        listed = search_miss_names()
        shown = ", ".join(names[:listed])
        more = f" and {len(names) - listed} more" if len(names) > listed else ""
        lines.append(
            f'No deferred tool matches "{query}". The deferred tools are: {shown}{more}. '
            "Search again by exact name (select:name) or other keywords."
        )
    return "\n".join(lines).strip(), structured


def _title(name: str) -> str:
    """A tool's display title (``view_pdf`` -> "View PDF"), or its name when it has none."""
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declared_tool_title,
    )

    return declared_tool_title(name) or name


def _action(count: int) -> str:
    return "Load tool" if count == 1 else "Load tools"


def _titles(titles: list[str]) -> str:
    """The row's subject: the first two titles, then how many more ("View PDF, Read docx +2")."""
    shown = ", ".join(titles[:2])
    return f"{shown} +{len(titles) - 2}" if len(titles) > 2 else shown


def _select_names(query: str) -> list[str]:
    if not query.strip().lower().startswith("select:"):
        return []
    return [name.strip() for name in query.strip()[7:].split(",") if name.strip()]


def _subject_view(action: str, subject: str, query: str) -> dict[str, Any]:
    """The row reads ``action`` then ``subject``; the query is body only when it adds to it."""
    blocks: list[dict[str, Any]] = [{"id": "loaded", "type": "text", "text": subject}]
    if query and query != subject:
        blocks.append({"id": "query", "type": "text", "label": "Search", "text": query})
    return {"action": action, "subject": "loaded", "blocks": blocks}


def _present(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    del result
    row = structured if isinstance(structured, Mapping) else {}
    tools = [t for t in row.get("tools") or [] if isinstance(t, Mapping)]
    query = str(row.get("query") or args.get("query") or "")
    titles = [str(t.get("title") or _title(str(t.get("name") or ""))) for t in tools]
    head = _subject_view(_action(len(tools)), _titles(titles) if tools else query, query)
    blocks: list[dict[str, Any]] = head["blocks"]
    for i, server in enumerate(row.get("servers") or []):
        if isinstance(server, Mapping) and server.get("instructions"):
            blocks.append(
                {
                    "id": f"server-{i}",
                    "type": "markdown",
                    "label": f"{server.get('name')} instructions",
                    "text": str(server["instructions"]),
                }
            )
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
    view: dict[str, Any] = {**head, "summary": summary, "blocks": blocks}
    return view | ({"status": status} if status else {})


def _starting(args: Mapping[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "")
    names = _select_names(query)
    titles = _titles([_title(n) for n in names]) if names else query
    return {**_subject_view(_action(len(names)), titles, query), "summary": ""}


def build_search_tools_tool() -> Any:
    """The ``search_tools`` native tool."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
        native_tool,
    )

    def search_tools(query: str, limit: int = 0) -> str:
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
        callers="direct",
        read_only=True,
        presentation=_present,
        presentation_start=_starting,
        domain="agents",
        title="Load tools",
        desc=(
            "Load tools named under 'Tools you load when needed' so you can call them. "
            "Query `select:name_a,name_b` for exact names, or keywords describing what you "
            "need (`+word more words` requires `word` in the tool name). The result gives "
            "each tool's description and argument schema, and the full instructions of the "
            "servers they belong to; call it from your next step. "
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
                f"{search_limit()}, at most {search_max()}).",
            },
        },
    )
