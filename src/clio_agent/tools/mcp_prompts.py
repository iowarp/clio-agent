"""The prompts an MCP server offers, over the agent's own connection to it.

A connected server's ``prompts/list`` is cached per connection: a reconnect is a
new connection era and lists again, and so does an unsolicited
``notifications/prompts/list_changed`` (:func:`mark_prompts_changed`, recorded by
:mod:`clio_agent.tools.mcp_listen`). A server that does not declare the prompts
capability offers none. ``prompts/get`` runs over the same connection.
"""

from __future__ import annotations

import threading
from typing import Any

from clio_agent.runtime import trace

#: A server announced that its prompt list changed.
MCP_PROMPTS_LIST_CHANGED = "mcp_prompts_list_changed"
#: A connected server does not declare the prompts capability: it offers none.
MCP_PROMPTS_NOT_OFFERED = "mcp_prompts_not_offered"

_LOCK = threading.Lock()
_GENERATIONS: dict[str, int] = {}


def mark_prompts_changed(namespace: str) -> int:
    """Record one ``prompts/list_changed`` for ``namespace``; return its new generation."""

    name = (namespace or "").strip()
    if not name:
        return 0
    with _LOCK:
        generation = _GENERATIONS.get(name, 0) + 1
        _GENERATIONS[name] = generation
    trace.event(
        "TOOLS", "%s namespace=%s generation=%d", MCP_PROMPTS_LIST_CHANGED, name, generation
    )
    return generation


def prompts_generation(namespace: str) -> int:
    with _LOCK:
        return _GENERATIONS.get(namespace, 0)


def offers_prompts(client: Any) -> bool:
    """Whether the connected server declared the prompts capability (unknown: ask it)."""

    capabilities = getattr(getattr(client, "initialize_result", None), "capabilities", None)
    return capabilities is None or getattr(capabilities, "prompts", None) is not None


async def list_prompts(client: Any, namespace: str, cache: dict[str, Any]) -> list[Any]:
    """``namespace``'s prompts over ``client``, cached per connection and change generation."""

    key = (id(client), prompts_generation(namespace))
    hit = cache.get(namespace)
    if hit is not None and hit[0] == key:
        return list(hit[1])
    if not offers_prompts(client):
        trace.event("TOOLS", "%s namespace=%s", MCP_PROMPTS_NOT_OFFERED, namespace)
        prompts: list[Any] = []
    else:
        prompts = list(await client.list_prompts())
    cache[namespace] = (key, prompts)
    return list(prompts)


__all__ = [
    "MCP_PROMPTS_LIST_CHANGED",
    "MCP_PROMPTS_NOT_OFFERED",
    "list_prompts",
    "mark_prompts_changed",
    "offers_prompts",
    "prompts_generation",
]
