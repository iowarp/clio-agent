"""Bounded session-context helpers for an agent-elicitation answer (#1309, C1-S7).

Split out of :mod:`clio_agent.gact.agent_elicitation` (the cleanup program's
no-accretion rule, #775: that module is a ratchet-baselined file — new logic
goes in an owner module of its own, not appended past its recorded line
count) as its own small, focused owner for exactly one concern: building the
bounded transcript excerpt and instructional prompt BOTH answer mechanisms
(the child-turn spawn and the inline completion) share. Moved verbatim, no
behavior change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.gact.elicitation_schema import FormTranslation
    from clio_agent.gact.types import UserQuestion

_CONTEXT_EXCERPT_MAX_CHARS = 6000


def _flatten_message_text(msg: Any) -> str:
    """Join a message's text parts (mirrors the same idiom used at every other
    "read a message's text back" call site in gact — deliberately small and
    duplicated here rather than reaching into another module's private helper)."""

    parts = getattr(msg, "parts", None) or []
    out: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text is None and isinstance(part, Mapping):
            text = part.get("text")
        part_type = getattr(part, "type", None) or (
            part.get("type") if isinstance(part, Mapping) else None
        )
        if part_type == "text" and text:
            out.append(str(text))
    return "".join(out).strip()


def _bounded_transcript_excerpt(app: Any, session_id: str) -> str:
    """A bounded, best-effort excerpt of ``session_id``'s own message transcript.

    NOT the ARC context-compilation pipeline (RULE 6): that plane
    (:mod:`clio_agent.arc.context_compiler`) has exactly one production
    consumer in this repo today (``arc/retrieval.py``) and is not wired into
    any gact turn/session call site, so depending on it here would be a new,
    unverified dependency rather than a reuse of "existing invocation
    machinery". This reads the session's own ALREADY-COLLECTED message store
    instead (RULE 4: no new store) and caps it so a long conversation cannot
    blow an unbounded prompt into the bounded answer turn -- the MOST RECENT
    text is kept (a truncated-from-the-front excerpt), since the nonce/fact the
    agent needs to recall is typically closer to the paused tool call.
    """

    messages = getattr(app.state, "messages", {})
    rows = messages.get(session_id, []) if hasattr(messages, "get") else []
    lines: list[str] = []
    for msg in rows:
        role = str(getattr(msg, "role", "") or "")
        if role not in ("user", "assistant"):
            continue
        text = _flatten_message_text(msg)
        if text:
            lines.append(f"{role}: {text}")
    excerpt = "\n".join(lines)
    if len(excerpt) > _CONTEXT_EXCERPT_MAX_CHARS:
        excerpt = excerpt[-_CONTEXT_EXCERPT_MAX_CHARS:]
    return excerpt


def _render_field(field: Mapping[str, Any]) -> str:
    piece = f"- {field.get('name')} ({field.get('type')}{'[]' if field.get('multi') else ''})"
    enum = field.get("enum")
    if enum:
        piece += f", one of {list(enum)}"
    if field.get("required"):
        piece += ", REQUIRED"
    description = field.get("description")
    if description:
        piece += f": {description}"
    return piece


def _build_answer_prompt(question: "UserQuestion", translation: "FormTranslation | None") -> str:
    """The instructional prompt handed to the bounded answer child turn."""

    fields = list(translation.fields) if translation is not None else []
    schema_desc = "\n".join(_render_field(f) for f in fields) or "(no fields declared)"
    return (
        "An MCP tool call in THIS conversation is paused, waiting for an answer that "
        "only you (the assistant, with this conversation's own context) can provide -- "
        "a human is not being asked because the server marked this question for the "
        "agent specifically.\n\n"
        f"Question: {question.prompt}\n\n"
        f"Answer fields:\n{schema_desc}\n\n"
        "Reply with EXACTLY ONE JSON object and nothing else (no prose, no markdown "
        "fences):\n"
        "- If the conversation above already establishes a confident answer, reply "
        '{"answer": {<one key per field name above, with a correctly-typed value>}}\n'
        '- If it does not, reply {"decline": true, "reason": "<short reason>"}\n'
        "Never guess -- decline unless the conversation genuinely established the answer."
    )
