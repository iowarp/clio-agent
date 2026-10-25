"""How long-term memory tool results read in the conversation.

A write (``create_memory``, ``update_memory``, ``forget_memory``) renders as a compact
memory notice: the fact itself (what was remembered, corrected or forgotten), "Memory
updated" and a human-readable memory title, which opens that fact
(``<document>#<fact id>``), no text box. Search and read show the documents they found, each linked to the page.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.memory.change_preview import changes_markdown, inline_change

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
    "already_remembered": "Already in memory",
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
        action = str(notice.get("action") or "")
        summary = _NOTICE.get(action, "Memory updated")
        fact = row.get("fact")
        text = str(fact.get("text") or "").strip() if isinstance(fact, Mapping) else ""
        if text:  # the row names what was remembered, corrected or forgotten
            previous = row.get("previous_fact")
            before = str(previous.get("text") or "") if isinstance(previous, Mapping) else ""
            changes = [
                {
                    "before": before
                    if action == "updated"
                    else text
                    if action == "forgotten"
                    else "",
                    "after": "" if action == "forgotten" else inline_change(text, text),
                }
            ]
            blocks.append(
                {
                    "id": "fact",
                    "type": "markdown",
                    "text": changes_markdown(changes)
                    if action != "already_remembered"
                    else inline_change(text, text),
                }
            )
        elif isinstance(row.get("forgotten_facts"), list):
            changes = [
                {"before": str(fact.get("text") or ""), "after": ""}
                for fact in row["forgotten_facts"]
                if isinstance(fact, Mapping)
            ]
            blocks.append({"id": "fact", "type": "markdown", "text": changes_markdown(changes)})
        fact_id = str(notice.get("fact_id") or "")
        if action == "forgotten":
            if not row.get("document_removed"):
                blocks.append(_link("memory", name, "Open in memory"))
        elif text:  # opens the memory page on this fact, to correct or forget it
            blocks.append(
                _link("memory", f"{name}#{fact_id}" if fact_id else name, "Edit in memory")
            )
        else:
            blocks.append(_link("memory", name, label))
        # The subject is the human title; changed facts remain visible content.
        if not any(block.get("id") == "memory" for block in blocks):
            blocks.append(
                {"id": "memory", "type": "text", "text": label}
                if row.get("document_removed")
                else _link("memory", name, label)
            )
        else:
            next(block for block in blocks if block.get("id") == "memory")["label"] = label
        return summary, blocks, "memory"
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
        if live:
            blocks.append(
                {
                    "id": "read-facts",
                    "type": "markdown",
                    "text": "\n".join(
                        f"- {inline_change(str(f.get('text') or ''), str(f.get('text') or ''))}"
                        for f in live
                    ),
                }
            )
        return f"{len(live)} {'fact' if len(live) == 1 else 'facts'}", blocks, "memory"
    if tool == "propose_memory_change":
        target = str(row.get("target") or "")
        blocks.append(
            _link(
                "memory",
                target,
                str(row.get("description") or target.rsplit("/", 1)[-1].replace("-", " ")),
            )
        )
        changes = row.get("changes")
        if isinstance(changes, list):
            blocks.append(
                {
                    "id": "memory-changes",
                    "type": "markdown",
                    "text": changes_markdown(
                        [change for change in changes if isinstance(change, dict)]
                    ),
                }
            )
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
