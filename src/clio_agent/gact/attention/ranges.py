"""Declare one labelled token range per transcript section of a rendered prompt.

The attention connector takes bare ``[lo, hi]`` token ranges and turns the gaps
between them into segments of their own. Labels never travel to vLLM: like
vllm-attn-connector's ``e2e_example.declare_ranges`` (whose vocabulary this
reuses -- the tool name for a tool block, ``"answer"`` for an earlier answer,
``"prose gap"`` for an undeclared gap), the declarer keeps them and joins them
back to the emitted segments by exact ``(lo, hi)``.

CLIO's prompts are DSPy ChatAdapter messages. Sections inside a message are the
adapter's own ``[[ ## field ## ]]`` wire markers -- a format CLIO's adapter
writes, parsed here at format level only (no reading of model prose). The field
-> domain mapping is the declared table :data:`FIELD_DOMAINS`; an unknown field
is labelled ``other`` with its field name, never guessed.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from clio_agent.gact.attention.chat_render import Encoded

#: Coarse section kinds shown in the Sources tab.
DOMAINS = (
    "system",
    "user",
    "tool_definitions",
    "tool_call",
    "tool_result",
    "thinking",
    "assistant_output",
    "template",
    "other",
)

#: DSPy field name -> (domain, label). Labels follow the e2e_example vocabulary.
FIELD_DOMAINS: dict[str, tuple[str, str]] = {
    "system_prompt": ("system", "system_prompt"),
    "question": ("user", "question"),
    "history": ("user", "history"),
    "images": ("user", "images"),
    "files": ("user", "files"),
    "tools": ("tool_definitions", "tools"),
    "next_thought": ("thinking", "next_thought"),
    "reasoning": ("thinking", "reasoning"),
    "tool_calls": ("tool_call", "tool_calls"),
    "next_tool_name": ("tool_call", "next_tool_name"),
    "next_tool_args": ("tool_call", "next_tool_args"),
    "tool_call_results": ("tool_result", "tool_call_results"),
    "observation": ("tool_result", "observation"),
    "answer": ("assistant_output", "answer"),
    "completed": ("template", "completed"),
}

#: DSPy ChatAdapter's fixed output-requirements preamble (its own template text,
#: not model prose): a user-role message that starts with it is adapter
#: instructions, not the scientist's words.
_ADAPTER_OUTPUT_PREAMBLE = "Respond with the corresponding output fields"

_FIELD_MARKER = re.compile(r"^\[\[ ## (\w+) ## \]\]", re.MULTILINE)
GAP_LABEL = "prose gap"


@dataclass(frozen=True)
class DeclaredRange:
    """One declared prompt section: token span, char span, and its labels."""

    lo: int
    hi: int
    domain: str
    label: str
    message_index: int
    char_lo: int
    char_hi: int

    def to_record(self) -> dict[str, Any]:
        """Plain dict for the ``lm.call`` provenance record."""
        return asdict(self)


@dataclass(frozen=True)
class Declaration:
    """All ranges for one request plus what is needed to verify them later."""

    ranges: list[DeclaredRange]
    prompt_token_count: int
    unlocated_messages: list[int]

    def wire_ranges(self) -> list[list[int]]:
        """The ``kv_transfer_params.ranges`` payload vLLM receives."""
        return [[r.lo, r.hi] for r in self.ranges]


def _content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI multi-part content: text parts only
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _char_sections(
    message: dict[str, Any], text: str, start: int, end: int
) -> list[tuple[int, int, str, str]]:
    """``(char_lo, char_hi, domain, label)`` sections of one located message."""
    role = str(message.get("role") or "")
    body = text[start:end]
    markers = list(_FIELD_MARKER.finditer(body))
    if not markers:
        if role == "system":
            return [(start, end, "system", "adapter_instructions")]
        if role == "user" and body.lstrip().startswith(_ADAPTER_OUTPUT_PREAMBLE):
            return [(start, end, "system", "adapter_instructions")]
        if role == "user":
            return [(start, end, "user", "user")]
        if role == "assistant":
            return [(start, end, "assistant_output", "answer")]
        if role == "tool":
            return [(start, end, "tool_result", str(message.get("name") or "tool"))]
        return [(start, end, "other", role or "message")]
    sections: list[tuple[int, int, str, str]] = []
    if role == "system":
        # The adapter's schema description: one system section, markers included.
        return [(start, end, "system", "adapter_instructions")]
    for i, match in enumerate(markers):
        field = match.group(1)
        field_lo = start + match.end()
        field_hi = start + (markers[i + 1].start() if i + 1 < len(markers) else len(body))
        lo, hi = _strip_span(text, field_lo, field_hi)
        if hi <= lo:
            continue
        domain, label = FIELD_DOMAINS.get(field, ("other", field))
        if field == "tool_call_results":
            sections.extend(_tool_result_entries(text, lo, hi))
        else:
            sections.append((lo, hi, domain, label))
    return sections


def _strip_span(text: str, lo: int, hi: int) -> tuple[int, int]:
    while lo < hi and text[lo].isspace():
        lo += 1
    while hi > lo and text[hi - 1].isspace():
        hi -= 1
    return lo, hi


def _tool_result_entries(text: str, lo: int, hi: int) -> list[tuple[int, int, str, str]]:
    """One section per entry of ``{"tool_call_results": [...]}``, labelled by tool name.

    Entry spans come from ``json.JSONDecoder.raw_decode`` end offsets, so they
    are exact. A body that is not that JSON shape stays one section.
    """
    decoder = json.JSONDecoder()
    body = text[lo:hi]
    whole = [(lo, hi, "tool_result", "tool_call_results")]
    key = body.find('"tool_call_results"')
    bracket = body.find("[", key) if key >= 0 else -1
    if bracket < 0:
        return whole
    pos = bracket + 1
    out: list[tuple[int, int, str, str]] = []
    while pos < len(body):
        while pos < len(body) and body[pos] in " \n\r\t,":
            pos += 1
        if pos >= len(body) or body[pos] == "]":
            break
        try:
            entry, end = decoder.raw_decode(body, pos)
        except json.JSONDecodeError:
            return whole
        name = entry.get("name") if isinstance(entry, dict) else None
        out.append((lo + pos, lo + end, "tool_result", str(name or "tool_result")))
        pos = end
    return out or whole


def declare_ranges(messages: list[dict[str, Any]], encoded: Encoded) -> Declaration:
    """Labelled token ranges for every section of ``messages`` in ``encoded``.

    Each message's content is located in the rendered string by a forward
    search (templates may trim content or strip earlier thinking, so offsets
    are never assumed). A message that cannot be located is reported in
    ``unlocated_messages``, not dropped silently.
    """
    text = encoded.text
    cursor = 0
    ranges: list[DeclaredRange] = []
    unlocated: list[int] = []
    for index, message in enumerate(messages):
        content = _content_text(message).strip()
        if not content:
            continue
        found = text.find(content, cursor)
        if found < 0:
            unlocated.append(index)
            continue
        end = found + len(content)
        cursor = end
        for char_lo, char_hi, domain, label in _char_sections(message, text, found, end):
            lo, hi = encoded.token_span(char_lo, char_hi)
            if ranges and lo < ranges[-1].hi:  # truncate overlaps, as the connector does
                lo = ranges[-1].hi
            if hi <= lo:
                continue
            ranges.append(DeclaredRange(lo, hi, domain, label, index, char_lo, char_hi))
    return Declaration(
        ranges=ranges, prompt_token_count=len(encoded.ids), unlocated_messages=unlocated
    )
