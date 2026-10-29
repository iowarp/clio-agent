"""Live tool lists for MCP servers that announce ``notifications/tools/list_changed``.

Some servers change their tool list while connected. googlecolab/colab-mcp starts with
one tool, ``open_colab_browser_connection``; once the Colab tab connects back to that
same process, the process proxies the notebook's tools and sends
``notifications/tools/list_changed``. Only the connection that received the
notification knows the new list: a fresh listing spawns a new process that has no
browser attached and still shows the single starting tool.

This module is the shared record between the two halves:

* the receiving side (:func:`clio_agent.tools.mcp_listen.list_changed_message_handler`,
  folded into every client :func:`~clio_agent.tools.mcp_runtime.make_mcp_client`
  builds for a known server, the gateway proxy backends included) calls
  :func:`mark_tools_changed`;
* an executor that has talked to the namespace re-lists it over its own persistent
  connection when :func:`tools_changed_generation` moved past what it last saw
  (:meth:`~clio_agent.tools.mcp_namespace_executor.AsyncNamespacePreparationMixin.
  refresh_changed_namespaces`), and replaces that namespace's definitions with
  :func:`prefixed_namespace_tools`, the same ``<namespace>_<tool>`` form a mount uses.

Every outcome is a typed trace reason: the change, each refresh, a refresh that could
not run, and a refresh that failed.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any

from clio_agent.runtime import trace

#: A server announced that its tool list changed (the notification arrived).
MCP_TOOLS_LIST_CHANGED = "mcp_tools_list_changed"
#: An executor re-listed a changed namespace over its persistent connection.
MCP_TOOLS_REFRESHED = "mcp_tools_refreshed"
#: The re-list could not reach the server (the old definitions stay, typed).
MCP_TOOLS_REFRESH_FAILED = "mcp_tools_refresh_failed"
#: A changed namespace this executor never connected to: its own process (if any)
#: did not send the notification, so there is nothing of its own to re-list.
MCP_TOOLS_REFRESH_NOT_CONNECTED = "mcp_tools_refresh_not_connected"

_LOCK = threading.Lock()
_GENERATIONS: dict[str, int] = {}


def mark_tools_changed(namespace: str) -> int:
    """Record one ``tools/list_changed`` for ``namespace``; return its new generation."""

    name = (namespace or "").strip()
    if not name:
        return 0
    with _LOCK:
        generation = _GENERATIONS.get(name, 0) + 1
        _GENERATIONS[name] = generation
    trace.event(
        "TOOLS",
        "%s namespace=%s generation=%d",
        MCP_TOOLS_LIST_CHANGED,
        name,
        generation,
    )
    return generation


def tools_changed_generation(namespace: str) -> int:
    """How many ``tools/list_changed`` notifications ``namespace`` has sent (0: none)."""

    with _LOCK:
        return _GENERATIONS.get(namespace, 0)


def changed_since(seen: Mapping[str, int], namespaces: Iterable[str]) -> dict[str, int]:
    """The namespaces among ``namespaces`` whose generation moved past ``seen``."""

    with _LOCK:
        current = {name: _GENERATIONS.get(name, 0) for name in namespaces}
    return {name: gen for name, gen in current.items() if gen > seen.get(name, 0)}


def prefixed_namespace_tools(namespace: str, listed: Iterable[Any]) -> dict[str, Any]:
    """Key bare listed tools as ``<namespace>_<tool>``, renamed the way a mount does."""

    return {
        f"{namespace}_{tool.name}": tool.model_copy(update={"name": f"{namespace}_{tool.name}"})
        for tool in listed
    }


def replace_namespace_entries(
    table: Mapping[str, Any], namespace: str, tools: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return ``table`` with ``namespace``'s entries replaced by ``tools``.

    Returns ``(new_table, added, removed)``: a tool the server no longer lists is
    dropped, so a removal reaches the agent as well as an addition.
    """

    prefix = f"{namespace}_"
    kept = {name: tool for name, tool in table.items() if not name.startswith(prefix)}
    before = {name for name in table if name.startswith(prefix)}
    added = sorted(set(tools) - before)
    removed = sorted(before - set(tools))
    return {**kept, **tools}, added, removed


def reset_for_tests() -> None:
    """Forget every recorded generation (test isolation seam)."""

    with _LOCK:
        _GENERATIONS.clear()


__all__ = [
    "MCP_TOOLS_LIST_CHANGED",
    "MCP_TOOLS_REFRESHED",
    "MCP_TOOLS_REFRESH_FAILED",
    "MCP_TOOLS_REFRESH_NOT_CONNECTED",
    "changed_since",
    "mark_tools_changed",
    "prefixed_namespace_tools",
    "replace_namespace_entries",
    "reset_for_tests",
    "tools_changed_generation",
]
