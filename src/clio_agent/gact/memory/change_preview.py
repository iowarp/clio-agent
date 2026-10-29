"""Fact changes from retained memory snapshots, shared by tools and review APIs."""

from __future__ import annotations

import difflib
import re
from typing import Any

from clio_agent.arc import memory_document as md


def fact_changes(before: str, after: str) -> list[dict[str, str]] | None:
    """Compare live facts using their explicit supersession links, never text similarity.

    ``None`` means the retained snapshot cannot be parsed; an empty list means
    both snapshots were readable and their live facts did not change.
    """
    try:
        old = md.parse(before) if before else None
        new = md.parse(after) if after else None
    except md.MemoryDocumentError:
        return None
    previous = {f.id: f for f in old.live_facts} if old else {}
    following = {f.id: f for f in new.facts} if new else {}
    live = {f.id: f for f in new.live_facts} if new else {}
    used: set[str] = set()
    changes = []
    for key, fact in previous.items():
        replacement = following.get(key)
        seen: set[str] = set()
        while replacement and replacement.superseded_by and replacement.id not in seen:
            seen.add(replacement.id)
            replacement = following.get(replacement.superseded_by)
        if replacement and replacement.live:
            used.add(replacement.id)
            if replacement.text != fact.text:
                changes.append(
                    {
                        "id": replacement.id,
                        "kind": "updated",
                        "before": fact.text,
                        "after": replacement.text,
                    }
                )
        else:
            changes.append({"id": key, "kind": "removed", "before": fact.text, "after": ""})
    changes.extend(
        {"id": f.id, "kind": "added", "before": "", "after": f.text}
        for f in live.values()
        if f.id not in used
    )
    return changes


def snapshot_label(before: str, after: str, fallback: str) -> str:
    """Use a recorded description, falling back to a readable subject name."""
    for text in (after, before):
        if text:
            try:
                return md.parse(text).description or fallback.rsplit("/", 1)[-1].replace("-", " ")
            except md.MemoryDocumentError:
                continue
    return fallback.rsplit("/", 1)[-1].replace("-", " ")


def _literal(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>#+.!|~])", r"\\\1", text)


def inline_change(before: str, after: str) -> str:
    """Render a literal, word-level correction as Markdown for generic readers."""
    old = re.findall(r"\w+|\s+|[^\w\s]+", before)
    new = re.findall(r"\w+|\s+|[^\w\s]+", after)
    pieces = []
    for operation, a, b, c, d in difflib.SequenceMatcher(
        a=old, b=new, autojunk=False
    ).get_opcodes():
        if operation == "equal":
            pieces.append(_literal("".join(old[a:b])))
        else:
            removed, added = "".join(old[a:b]), "".join(new[c:d])
            if removed.strip():
                pieces.append(f"~~{_literal(removed.strip())}~~")
            if removed.strip() and added.strip():
                pieces.append(" ")
            if added.strip():
                pieces.append(f"**{_literal(added.strip())}**")
    return "".join(pieces)


def changes_markdown(changes: list[dict[str, Any]]) -> str:
    """A compact literal list; rich clients can render the same captured facts."""
    lines = []
    for change in changes:
        before, after = str(change.get("before") or ""), str(change.get("after") or "")
        if before and after:
            lines.append(f"- {inline_change(before, after)}")
        elif before:
            lines.append(f"- Removed: ~~{_literal(before)}~~")
        elif after:
            lines.append(f"- Added: {_literal(after)}")
    return "\n".join(lines)
