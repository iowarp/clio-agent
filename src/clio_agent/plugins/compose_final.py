"""The prompt an agent actually gets: only the fragments its final tools need.

A fragment's ``when`` is first evaluated at resolve time against the tools the plugins
provide (:func:`clio_agent.plugins.compose.fragment_applies`). A session then narrows
those tools: memory policy withholds some, a read-only child loses the ones that write,
an allowlist keeps a few. :func:`final_prompt_text` re-evaluates every fragment against
the tools the agent can call at the end of that narrowing, so an agent is never told
about a tool it cannot call, and applies the fragment's ``when.session`` scope (``root``
guidance is for a session a person reads, ``delegated`` guidance for one another agent
started). Tools that are callable but not listed (deferred until searched) count as
callable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["final_prompt_text", "fragment_reaches"]


def fragment_reaches(
    fragment: Mapping[str, Any], callable_names: set[str], *, delegated: bool
) -> bool:
    """Whether a composed fragment belongs in the prompt of an agent with these tools.

    A ``when_tools`` entry holds when a callable tool has exactly that name or starts
    with ``<entry>_`` (an MCP or built-in namespace such as ``shell``).
    """
    if fragment.get("session") == ("root" if delegated else "delegated"):
        return False
    for wanted in fragment.get("when_tools") or ():
        if wanted not in callable_names and not any(
            name.startswith(f"{wanted}_") for name in callable_names
        ):
            return False
    return True


def _fragments(prompt: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        fragment
        for section in prompt.get("sections", [])
        for fragment in section.get("fragments", [])
        if fragment.get("text")
    ]


def final_prompt_text(
    text: str,
    prompt: Mapping[str, Any],
    callable_names: Iterable[str],
    *,
    delegated: bool,
) -> str:
    """``text``, composed from ``prompt``, keeping only the fragments that reach this agent.

    When ``text`` is exactly ``prompt``'s composition it is recomposed from the kept
    fragments. A text with more around it (a prompt basis held across a plugin change)
    loses each dropped fragment's paragraphs instead.
    """
    names = set(callable_names)
    fragments = _fragments(prompt)
    kept = [f for f in fragments if fragment_reaches(f, names, delegated=delegated)]
    if len(kept) == len(fragments):
        return text
    if text == "\n\n".join(str(f["text"]) for f in fragments):
        return "\n\n".join(str(f["text"]) for f in kept)
    for fragment in fragments:
        if fragment in kept:
            continue
        body = str(fragment["text"])
        text = "\n\n".join(part for part in text.split("\n\n") if part != body)
        if body in text:  # a multi-paragraph fragment
            text = text.replace(f"\n\n{body}", "").replace(body, "")
    return text
