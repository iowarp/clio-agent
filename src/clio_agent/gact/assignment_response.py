"""Public response parts carried by a conversation assignment's durable result.

The final prediction is a value, not a transcript: asynchronous arrivals can make
an agent follow a substantial response with an addendum. Preserve both without
rewriting the final value or forwarding provider reasoning and tool payloads.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from clio_agent.gact.parts import Part

_PUBLIC_METADATA = frozenset(
    {"signature_field_name", "revises", "revised_by", "reason", "superseded_by"}
)


def response_parts(parts: Iterable[Part], agent_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Project the responder's authored text, in order, with revision identities.

    Superseded parts remain inspectable. A consumer presenting the current public
    response must respect ``superseded_by``. Private thinking, child-agent prose,
    tool results and runtime notices are not the responder's authored response.
    """

    authors = set(agent_ids)
    return [
        {
            "id": part.id,
            "type": "text",
            "agent_id": part.agent_id,
            "text": part.text,
            "metadata": {
                key: value for key, value in part.metadata.items() if key in _PUBLIC_METADATA
            },
        }
        for part in parts
        if part.type == "text" and part.agent_id in authors and part.text
    ]


def merge_response_parts(
    earlier: list[dict[str, Any]], later: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Retain parts across a yield/resume, identifying repeated parts by id."""

    by_id = {part["id"]: part for part in [*earlier, *later]}
    return list(by_id.values())
