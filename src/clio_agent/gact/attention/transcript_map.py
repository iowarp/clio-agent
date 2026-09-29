"""Prompt positions -> transcript characters.

The prompt a call saw is DSPy-rendered text, not the transcript. Each
transcript text (a user message, an earlier answer, a tool call's thought or
input, a tool result) is located in the rendered prompt by exact string
equality -- verbatim or JSON-escaped (:mod:`.textmap`) -- inside a section of a
compatible domain. A located text gets per-token heat runs in its own char
coordinates; a text that is not in the prompt (the model never saw it, or saw a
transformed copy) is simply not located, and the Sources view still counts the
section it would have been in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from clio_agent.gact.attention.aggregate import Mass, Section
from clio_agent.gact.attention.chat_render import Encoded
from clio_agent.gact.attention.textmap import Located, locate_all

#: Transcript text kind -> section domains it may be located in. Earlier turns'
#: content re-enters later prompts inside the ``question`` / ``history`` fields,
#: hence ``user`` everywhere.
_ALLOWED_DOMAINS: dict[str, frozenset[str]] = {
    "user_text": frozenset({"user"}),
    "assistant_text": frozenset({"user", "assistant_output", "tool_call"}),
    "thought": frozenset({"thinking", "user"}),
    "tool_input": frozenset({"tool_call", "user"}),
    "tool_result": frozenset({"tool_result", "user"}),
}


@dataclass(frozen=True)
class TranscriptText:
    """One selectable-or-attendable text of the transcript."""

    message_id: str
    part_id: str
    field: str
    kind: str
    text: str


def transcript_texts(messages: list[Any]) -> list[TranscriptText]:
    """Every text of ``messages`` (gact ``Message`` objects) the prompt could contain."""
    out: list[TranscriptText] = []
    for message in messages:
        role = getattr(message, "role", "")
        for part in getattr(message, "parts", []) or []:
            ptype = getattr(part, "type", "")
            pid = getattr(part, "id", "") or ""
            if ptype == "text" and getattr(part, "text", ""):
                kind = "user_text" if role == "user" else "assistant_text"
                out.append(TranscriptText(message.id, pid, "text", kind, part.text))
            elif ptype == "tool_call":
                if getattr(part, "thought", ""):
                    out.append(TranscriptText(message.id, pid, "thought", "thought", part.thought))
                if getattr(part, "input", None):
                    text = json.dumps(part.input, ensure_ascii=False)
                    out.append(TranscriptText(message.id, pid, "input", "tool_input", text))
            elif ptype == "tool_result":
                text = getattr(part, "text", "") or "\n".join(
                    getattr(c, "text", "") for c in getattr(part, "content", []) or []
                )
                if text:
                    out.append(TranscriptText(message.id, pid, "result", "tool_result", text))
    return out


@dataclass(frozen=True)
class Anchor:
    """A transcript text located in the prompt."""

    text: TranscriptText
    hit: Located
    section_index: int
    token_lo: int
    token_hi: int


def _section_at(sections: list[Section], token: int) -> int:
    for i, section in enumerate(sections):
        if section.lo <= token < section.hi:
            return i
    return -1


def anchor_texts(
    texts: list[TranscriptText], encoded: Encoded, sections: list[Section]
) -> list[Anchor]:
    """Locate each transcript text in the rendered prompt (first compatible hit)."""
    anchors: list[Anchor] = []
    for item in texts:
        allowed = _ALLOWED_DOMAINS.get(item.kind, frozenset())
        for hit in locate_all(encoded.text, item.text):
            tok_lo, tok_hi = encoded.token_span(hit.start, hit.end)
            index = _section_at(sections, tok_lo)
            if index >= 0 and sections[index].domain in allowed:
                anchors.append(Anchor(item, hit, index, tok_lo, tok_hi))
                break
    return anchors


def heat_runs(anchor: Anchor, encoded: Encoded, mass: Mass) -> list[list[float]]:
    """``[char_lo, char_hi, value]`` in the transcript text for retained tokens."""
    runs: list[list[float]] = []
    for token in range(anchor.token_lo, anchor.token_hi):
        value = float(mass.mean[token])
        if value <= 0.0:
            continue  # not in the top-N% for these steps; the UI shows the residual
        a, b = encoded.offsets[token]
        span = anchor.hit.from_haystack(a, b)
        if span is not None:
            runs.append([span[0], span[1], value])
    return runs


def block_stats(anchor: Anchor, mass: Mass) -> dict[str, float]:
    """Total and mean-per-token retained mass for one located transcript text."""
    width = max(1, anchor.token_hi - anchor.token_lo)
    total = float(mass.mean[anchor.token_lo : anchor.token_hi].sum())
    return {"share": total, "mean": total / width, "tokens": float(width)}
