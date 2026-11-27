"""Whose server a name is: the session's plugin's, or the person's own (user/workspace).

A name a plugin declares stays the plugin's: a person's ``mcp.yaml`` entry with the same
name takes its place only when it says ``replaces_plugin_server: true`` (a remote
deployment of that server, say). Any other same-named entry is left out of that
session's fleet, logged with ``mcp_server_name_taken_by_plugin`` and reported to the
caller, never served in the plugin's place: a plugin's tools must reach its own process.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any

from clio_agent.errors import MCP_SERVER_NAME_TAKEN_BY_PLUGIN

logger = logging.getLogger(__name__)


def merge_person_specs(
    merged: MutableMapping[str, Any],
    person: Mapping[str, Any],
    path_of: Mapping[str, Any],
    taken: list[dict[str, str]] | None,
) -> None:
    """Merge the person's ``person`` specs into ``merged`` (plugin specs, by name).

    A plugin spec's source is ``plugin:<id>``; ``path_of`` names the ``mcp.yaml`` of each
    person source (``user``, ``workspace``). A left-out entry is appended to ``taken`` as
    ``{"reason", "server", "plugin", "source", "path"}``.
    """
    for name, spec in person.items():
        held = str(getattr(merged.get(name), "source", "") or "")
        if not held.startswith("plugin:"):
            merged[name] = spec
            continue
        plugin = held.removeprefix("plugin:")
        source = str(getattr(spec, "source", ""))
        if getattr(spec, "replaces_plugin_server", False):
            merged[name] = spec
            logger.info(
                "mcp server declaration replaces plugin server server=%s plugin=%s source=%s",
                name,
                plugin,
                source,
            )
            continue
        row = {
            "reason": MCP_SERVER_NAME_TAKEN_BY_PLUGIN,
            "server": name,
            "plugin": plugin,
            "source": source,
            "path": str(path_of.get(source, "")),
        }
        logger.warning(
            "mcp server declaration left out reason=%s server=%s plugin=%s source=%s path=%s",
            *row.values(),
        )
        if taken is not None:
            taken.append(row)


__all__ = ["merge_person_specs"]
