"""Guidance in tool responses: the ``tool_result`` event, appended to the result text.

After each tool call the loop fires ``tool_result`` with the variable ``tool``
(``name``, ``args``, ``is_error``, ``chars``). Each active injection on it whose ``tool:``
pattern matches is appended to the text the model reads, after a blank line, and noted
for the user (:mod:`clio_agent.injections.notes`). This is how a plugin gives guidance
where a situation arises instead of in the system prompt. A result that is not text
(a structured value) carries no guidance; that is traced as ``tool_guidance_not_text``.
"""

from __future__ import annotations

from typing import Any

from clio_agent.injections.resolution import fire
from clio_agent.runtime import trace

__all__ = ["NOT_TEXT", "after_tool_result"]

#: Typed trace reason: guidance fired for a result that is not text, so it was not added.
NOT_TEXT = "tool_guidance_not_text"


def after_tool_result(
    name: str, args: dict[str, Any], value: Any, is_error: bool
) -> tuple[Any, list[tuple[str, str]]]:
    """``(value with guidance appended, notes)`` for one finished tool call."""
    chars = len(value) if isinstance(value, str) else 0
    variables = {"tool": {"name": name, "args": args, "is_error": is_error, "chars": chars}}
    notes = fire("tool_result", variables, tool=name)
    if not notes:
        return value, []
    if not isinstance(value, str):
        trace.event("INJECTION", "%s tool=%s count=%d", NOT_TEXT, name, len(notes))
        return value, []
    joined = "\n\n".join([value, *(text for _source, text in notes)])
    if type(value) is not str:  # a marked text (a collected task reply) keeps its marks
        marked = str.__new__(type(value), joined)
        marked.__dict__.update(value.__dict__)
        return marked, notes
    return joined, notes
