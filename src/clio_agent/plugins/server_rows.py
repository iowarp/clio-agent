"""The resolution row of one MCP server a plugin declares.

:func:`server_row` is what ``resolve`` records under ``mcp_servers`` for each
server: its owning plugin, spec, disclosure (defer, description, callers,
permission, per-tool settings) and, when the plugin declares any, the host
semantics slice that server gets (``host``, see :mod:`.host_semantics`).
"""

from __future__ import annotations

from typing import Any

from clio_agent.plugins.host_semantics import server_host
from clio_agent.plugins.records import PluginRecord


def server_row(record: PluginRecord, name: str, spec: Any) -> dict[str, Any]:
    """The ``mcp_servers`` row of ``record``'s server ``name``."""

    disclosed = (record.disclosure.get("servers") or {}).get(name) or {}
    row: dict[str, Any] = {
        "plugin": record.id,
        "root": record.root,
        "spec": spec,
        "defer": disclosed.get("defer", record.disclosure.get("defer")),
        "description": disclosed.get("description", ""),
        "callers": disclosed.get("callers"),
        "permission": disclosed.get("permission"),
        "tools": dict(disclosed.get("tools") or {}),
    }
    if host := server_host(record, name):
        row["host"] = host
    return row


__all__ = ["server_row"]
