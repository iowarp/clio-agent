"""The agent's context as the model receives it, for the context route to show.

A scope's live segments fold into the same messages the loop sends
(:func:`~clio_agent.gact.agents.clio_react_record.fold_steps`), so what an
inspector sees is exactly the agent's context: its user messages and CLIO's
additions, each step's thinking, text and tool calls, and the results. Media is
named by type only; its bytes stay out of an inspection payload.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def context_messages(segments: Sequence[Any]) -> list[dict[str, Any]]:
    """Fold ordered live segments into the agent's messages, as JSON-ready dicts."""
    from clio_agent.gact.agents.clio_react_record import fold_steps  # noqa: PLC0415

    return [
        {"role": message.role, "parts": [_part(p) for p in message.parts]}
        for message in fold_steps(segments)
    ]


def _part(part: Any) -> dict[str, Any]:
    kind = str(getattr(part, "type", ""))
    if kind in ("text", "thinking"):
        return {"type": kind, "text": part.text}
    if kind == "tool_call":
        return {"type": kind, "id": part.id, "name": part.name, "input": dict(part.input)}
    if kind == "tool_result":
        return {
            "type": kind,
            "id": part.id,
            "name": part.name,
            "is_error": bool(part.is_error),
            "content": [_part(p) for p in part.content],
        }
    if kind in ("image", "document"):
        return {"type": kind, "media_type": part.media_type}
    raise ValueError(f"context view: unknown message part type {kind!r}")
