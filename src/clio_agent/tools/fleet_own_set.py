"""A server set that keeps a person's server name for its plugin gets a fleet of its own.

A plugin keeps its server's name (:mod:`clio_agent.tools.mcp_person_precedence`): in a
session with that plugin, ``probe`` is the plugin's process; in a session without it,
``probe`` is the person's own ``mcp.yaml`` server. A workspace fleet runs one process per
name, so the two cannot share one: the first-mounted spec would serve the other
session's calls. Such a server set is filed under a fleet key of its own at that root
(:func:`clio_agent.tools.fleet_key.fleet_key`), as a conversation with its own
connector values is. Every session with that same set shares it.

The test is the precedence rule itself: the set's declarations are merged with the
person's ``mcp.yaml`` files exactly as the set's gateway will merge them, and a name
left out (``mcp_server_name_taken_by_plugin``) means the set shadows one. The answer is
cached until one of those files, or the workspace trust store, changes.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

#: Between a set's connector-values variant and its own-fleet mark in a fleet variant.
OWN_FLEET_MARK = "+set="

_CACHE: dict[tuple[object, ...], bool] = {}
_LOCK = threading.Lock()


def fleet_variant(server_set: str, root: str) -> str:
    """The fleet variant of ``server_set`` at workspace ``root`` (see the module doc).

    The connector-values variant of the set, plus the set's own mark when it shadows a
    person's server there. "" is the workspace's plain fleet.
    """

    from clio_agent.plugins.servers import VARIANT_MARK, server_set_variant  # noqa: PLC0415

    variant = server_set_variant(server_set)
    if not server_set or not root or not shadows_person_server(server_set, root):
        return variant
    return f"{variant}{OWN_FLEET_MARK}{server_set.partition(VARIANT_MARK)[0]}"


def shadows_person_server(server_set: str, root: str) -> bool:
    """Whether ``server_set`` keeps a name a person's ``mcp.yaml`` at ``root`` also uses."""

    from clio_agent.plugins.servers import server_set_declarations  # noqa: PLC0415
    from clio_agent.tools.mcp_config import load_mcp_servers  # noqa: PLC0415

    key = (server_set, root, *_stamps(root))
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    try:
        declared = server_set_declarations(server_set)
    except KeyError:
        # An unregistered set mounts nothing: its gateway build fails on its own path.
        return False
    taken: list[dict[str, str]] = []
    load_mcp_servers(cwd=root, plugin_servers=declared, taken=taken)
    with _LOCK:
        _CACHE[key] = bool(taken)
    return bool(taken)


def _stamps(root: str) -> tuple[object, ...]:
    """What the answer depends on: the person's two ``mcp.yaml`` files and the trust store."""

    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.tools.workspace_mcp_trust import trust_store_path  # noqa: PLC0415

    user = paths.user_config_dir_for(Path.home(), os.environ) / "mcp.yaml"
    workspace = paths.workspace_config_path(root, "mcp.yaml")
    return tuple(_stat(path) for path in (user, workspace, trust_store_path()))


def _stat(path: Path) -> tuple[str, int, int]:
    try:
        info = path.stat()
    except OSError:
        return (str(path), -1, -1)
    return (str(path), info.st_mtime_ns, info.st_size)


__all__ = ["OWN_FLEET_MARK", "fleet_variant", "shadows_person_server"]
