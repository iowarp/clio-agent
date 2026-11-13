"""A connector's tools as last listed while it was ready (CN5).

A conversation must keep seeing a connector's tools when it stops being ready (the
person signed out, a secret was cleared): that is when a call waits for the setup
card instead of failing (CN5). So each live listing of a connector's namespace is
kept here, and :func:`last_listed` serves it when a listing fails because the
connector needs something, with the typed trace ``connector_tools_from_last_listing``.
Tool definitions carry no credential.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from clio_agent import paths
from clio_agent.runtime import trace


def _path(connector_id: str) -> Path:
    return paths.user_cache_dir() / "connectors" / f"{connector_id}.tools.json"


def remember(connector_id: str, tools: Iterable[Any]) -> None:
    """Keep the bare-named tool definitions of a live listing."""

    rows = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        if hasattr(tool, "model_dump")
        else dict(tool)
        for tool in tools
    ]
    path = _path(connector_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows), encoding="utf-8")
    except OSError as exc:
        trace.event("TOOLS", "connector_tools_not_kept connector=%s reason=%s", connector_id, exc)


def last_listed(connector_id: str) -> list[Any]:
    """The kept definitions as MCP ``Tool`` objects ([] when none were kept)."""

    from mcp.types import Tool  # noqa: PLC0415

    try:
        rows = json.loads(_path(connector_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Tool.model_validate(row) for row in rows if isinstance(row, dict)]


def chain_names(error: BaseException) -> str:
    """The exception types ``error`` wraps, for a typed trace."""

    names: list[str] = []
    pending: list[BaseException] = [error]
    while pending and len(names) < 16:
        current = pending.pop()
        names.append(type(current).__name__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(item for item in (current.__cause__, current.__context__) if item)
    return ">".join(names)


def needs_setup(error: BaseException) -> bool:
    """Whether ``error`` (or anything it wraps) says the connection needs a sign-in."""

    from clio_agent.tools.mcp_sign_in import MCPSignInNeeded  # noqa: PLC0415

    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, MCPSignInNeeded):
            return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(item for item in (current.__cause__, current.__context__) if item)
    return False


def held_for_setup(connector_id: str, error: BaseException) -> bool:
    """Whether a failed listing of ``connector_id`` is the connector waiting on the person.

    Either ``error`` wraps the typed sign-in error, or CLIO's own record of the
    connector says a required value, secret or sign-in is missing. The second matters
    when the listing went through a proxy, which hands the error back as a protocol
    error and so drops its type (AQ6).
    """

    if needs_setup(error):
        return True
    from clio_agent.connectors import state  # noqa: PLC0415
    from clio_agent.gact.connectors_service import missing  # noqa: PLC0415

    entry = state.all_entries().get(connector_id)
    return entry is not None and bool(missing(entry, state.choice(connector_id)))


def offered(connector_id: str, tools: list[Any]) -> list[Any]:
    """The listed tools (MCP ``Tool`` objects or rows) the connector's entry offers.

    All of them when the entry names none.

    A name the entry lists that the server does not have is a typed trace,
    ``connector_tool_not_listed``, never a silent drop.
    """

    from clio_agent.connectors import state  # noqa: PLC0415

    entry = state.all_entries().get(connector_id)
    allowed = tuple(getattr(entry, "tools", ()) or ())
    if not allowed:
        return tools
    names = {_name(tool) for tool in tools}
    for name in allowed:
        if name not in names:
            trace.event(
                "TOOLS", "connector_tool_not_listed connector=%s tool=%s", connector_id, name
            )
    return [tool for tool in tools if _name(tool) in allowed]


def _name(tool: Any) -> str:
    """A tool's name, from an MCP ``Tool`` or a listed row."""

    return str(tool.get("name") or "") if isinstance(tool, dict) else str(getattr(tool, "name", ""))
