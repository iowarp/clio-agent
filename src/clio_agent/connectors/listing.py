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


def needs_setup(error: BaseException) -> bool:
    """Whether ``error`` (or anything it wraps) says the connection needs a sign-in."""

    from clio_agent.tools.mcp_config import MCPSignInNeeded  # noqa: PLC0415

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
