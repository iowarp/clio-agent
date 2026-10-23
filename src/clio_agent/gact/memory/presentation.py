"""How long-term memory tool results read in the conversation.

A write (``create_memory``, ``update_memory``, ``forget_memory``) renders as a compact
memory notice: "Memory updated" and a link to the document on the memory page, no
text box. Search and read show the documents they found, each linked to the page.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LONG_TERM_TOOLS = frozenset(
    {
        "memory_search",
        "memory_read",
        "create_memory",
        "update_memory",
        "forget_memory",
        "propose_memory_change",
    }
)
_NOTICE = {
    "created": "Memory updated",
    "updated": "Memory updated",
    "forgotten": "Memory forgotten",
}


def _link(block_id: str, name: str, label: str) -> dict[str, Any]:
    return {"id": block_id, "type": "link", "target": "memory", "uri": name, "label": label}


def present_long_term_memory(
    tool: str, args: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[str, list[dict[str, Any]], str]:
    """``(summary, blocks, subject)`` for one long-term memory tool result."""
    blocks: list[dict[str, Any]] = []
    notice = row.get("memory")
    if isinstance(notice, Mapping):
        name = str(notice.get("name") or "")
        label = str(notice.get("description") or name)
        blocks.append(_link("memory", name, label))
        return _NOTICE.get(str(notice.get("action") or ""), "Memory updated"), blocks, "memory"
    if tool == "memory_search":
        hits = [hit for hit in row.get("hits", []) if isinstance(hit, Mapping)]
        for index, hit in enumerate(hits):
            blocks.append(
                _link(
                    f"hit-{index}",
                    str(hit.get("name") or ""),
                    str(hit.get("description") or hit.get("name") or ""),
                )
            )
        query = str(row.get("query") or args.get("query") or "")
        count = len(hits)
        summary = (
            f"{count} {'document' if count == 1 else 'documents'}" if hits else "Nothing in memory"
        )
        if query:
            blocks.insert(0, {"id": "query", "type": "text", "text": query})
        return summary, blocks, "query" if query else ""
    if tool == "memory_read":
        name = str(row.get("name") or args.get("name") or "")
        blocks.append(_link("memory", name, str(row.get("description") or name)))
        live = [f for f in row.get("facts", []) if isinstance(f, Mapping) and f.get("live", True)]
        return f"{len(live)} {'fact' if len(live) == 1 else 'facts'}", blocks, "memory"
    if tool == "propose_memory_change":
        target = str(row.get("target") or "")
        blocks.append(_link("memory", target, target))
        return f"Proposed: {row.get('change', '')} ({row.get('status', '')})", blocks, "memory"
    return "", blocks, ""


def memory_used_projection(metadata: Mapping[str, Any]) -> dict[str, list[str]] | None:
    """The memory documents an answer read or had loaded: names only, for its popover."""
    used = metadata.get("memory_used")
    if not isinstance(used, Mapping):
        return None
    names = {
        key: [name for name in used.get(key) or [] if isinstance(name, str)]
        for key in ("read", "loaded")
    }
    return names if names["read"] or names["loaded"] else None


__all__ = ["LONG_TERM_TOOLS", "memory_used_projection", "present_long_term_memory"]
