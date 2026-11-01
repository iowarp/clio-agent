"""What each MCP server says about itself (``instructions``), kept per namespace.

A server may send ``instructions`` when a client connects. CLIO keeps them per
namespace from the live listing (:func:`record_listing_facts`), persists them with the
listing cache entry, and replays them on a cache hit, so a server deferred behind
``search_tools`` is described in the prompt by its own words when its plugin gives no
``servers.<name>.description`` (:mod:`clio_agent.gact.agents.deferred_tools`).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

__all__ = ["instructions_for", "record_instructions", "record_listing_facts"]

_LOCK = threading.Lock()
_BY_NAMESPACE: dict[str, str] = {}
#: How much of a server's instructions a prompt entry uses.
_SUMMARY_CHARS = 300


def record_instructions(namespace: str, instructions: str | None) -> None:
    """Keep (or, with nothing, forget) a namespace's server instructions."""
    text = str(instructions or "").strip()
    with _LOCK:
        if text:
            _BY_NAMESPACE[namespace] = text
        else:
            _BY_NAMESPACE.pop(namespace, None)


def instructions_for(namespace: str, *, summary: bool = False) -> str:
    """The namespace's instructions; ``summary`` gives the first paragraph, bounded."""
    with _LOCK:
        text = _BY_NAMESPACE.get(namespace, "")
    if not summary or not text:
        return text
    first = text.split("\n\n", 1)[0].strip()
    return first if len(first) <= _SUMMARY_CHARS else first[: _SUMMARY_CHARS - 1].rstrip() + "…"


def record_listing_facts(namespace: str, client: Any, tools: Sequence[Any]) -> None:
    """Everything a live listing establishes: the task capability and the instructions."""
    from clio_agent.tools.mcp_task_routing import record_definitive_capability  # noqa: PLC0415

    record_definitive_capability(namespace, client, tools)
    record_instructions(namespace, getattr(client, "instructions", None))
