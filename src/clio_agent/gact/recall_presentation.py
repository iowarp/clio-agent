"""How a ``recall_context`` call reads: what was asked, then the steps it brought back.

The row's target is the query, or the step or compaction ids asked for. The summary is
the recall's own count ("2 of 2 steps"). Each step is a block labelled with its kind
(said, a call, a result; "compacted" when a summary had replaced it), its text as the
agent got it, and its step id as detail. Notes about steps left out or not shown follow.
The exact result stays in the technical details.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = ["present_recall"]

#: A step's header line in the recall text: ``--- <id> (<kind>[, compacted])``.
_STEP = re.compile(r"^--- (?P<id>\S+) \((?P<label>[^)\n]*)\)$", re.MULTILINE)
#: The recall text's step kinds as a reader names them.
_KINDS = {"said": "Agent said", "call": "Tool call", "result": "Tool result", "message": "Message"}


def _text(result: Any) -> str:
    """The recall text from the shapes a native result arrives in."""
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        value = result.get("result", result.get("text"))
        return value if isinstance(value, str) else ""
    if isinstance(result, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in result
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def present_recall(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """The recall as a target, a count, and one block per step brought back."""
    del structured
    text = _text(result)
    blocks: list[dict[str, Any]] = []
    query = str(args.get("query") or "").strip()
    ids = [str(value) for value in args.get("ids") or [] if str(value).strip()]
    target = query or (ids[0] if len(ids) == 1 else f"{len(ids)} steps by id" if ids else "")
    if target:
        blocks.append({"id": "target", "type": "text", "label": target, "text": target})
    steps = list(_STEP.finditer(text))
    head = text[: steps[0].start()].strip() if steps else text.strip()
    # The head's first sentence is the count; the rest notes what was left out.
    count, _, notes = head.partition(". ")
    summary = count.split(",")[0].strip() if steps or count else ""
    for index, step in enumerate(steps):
        end = steps[index + 1].start() if index + 1 < len(steps) else len(text)
        kind, _, compacted = step["label"].strip().partition(",")
        label = _KINDS.get(kind.strip(), kind.strip().capitalize())
        blocks.append(
            {
                "id": f"step-{index}",
                "type": "text",
                "label": f"{label}, compacted" if compacted.strip() else label,
                "text": text[step.end() : end].strip(),
                "detail": step["id"],
            }
        )
    if notes.strip():
        blocks.append({"id": "notes", "type": "text", "text": notes.strip()})
    if not steps and head and not summary:
        blocks.append({"id": "content", "type": "text", "text": head})
    return {
        "subject": "target" if target else None,
        "summary": summary or ("Nothing to recall" if not text.strip() else ""),
        "blocks": blocks,
    }
