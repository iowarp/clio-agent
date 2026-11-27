"""A session's plugin MCP servers start from the copy installed now.

A session pins the plugin set it resolved, but a plugin's ``PLUGIN_DATA`` folder is
keyed by the plugin alone (:func:`clio_agent.plugins.servers.plugin_data_dir`), so
every copy of one plugin shares it. A session still pinned to an older copy (one not
open when the plugin was updated, warmed at agent start, a child, a tab) would start
that older server next to the current one, on the same data, and two versions of one
server would act on one state. So the server set a pin mounts takes each plugin's
servers from its installed copy when that copy has moved on: the session's servers
are upgraded, and a moved copy not trusted yet is not started at all. The pinned
prompt, skills and tools are untouched; :mod:`clio_agent.gact.plugin_update_follow`
moves those when the session is next pinned. Nothing here names a plugin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from clio_agent.plugins.records import PluginRecord
from clio_agent.plugins.server_rows import server_row


def follow_installed(
    mcp_servers: Mapping[str, Mapping[str, Any]],
    records: Iterable[PluginRecord],
    trusted: Callable[[PluginRecord], bool],
) -> dict[str, Mapping[str, Any]]:
    """``mcp_servers`` with each moved plugin's rows taken from its installed copy.

    A row's copy has moved when no installed record of its plugin has its root and
    exactly one installed record of that plugin exists (the data folder is keyed by the
    plugin id, so that record is the one sharing it). An uninstalled plugin, or an id
    two markets both install, keeps its pinned rows. A moved copy ``trusted`` refuses
    starts no server of that plugin.
    """

    installed: dict[str, list[PluginRecord]] = {}
    for record in records:
        installed.setdefault(record.id, []).append(record)
    moved: dict[str, PluginRecord] = {}
    for row in mcp_servers.values():
        plugin = str(row.get("plugin") or "")
        copies = installed.get(plugin) or []
        if len(copies) != 1 or any(copy.root == str(row.get("root") or "") for copy in copies):
            continue
        moved[plugin] = copies[0]
    if not moved:
        return dict(mcp_servers)
    out = {
        name: row for name, row in mcp_servers.items() if str(row.get("plugin") or "") not in moved
    }
    for _plugin, record in sorted(moved.items()):
        if not trusted(record):
            continue
        for name, spec in record.mcp_servers.items():
            out.setdefault(name, server_row(record, name, spec))
    return out


__all__ = ["follow_installed"]
