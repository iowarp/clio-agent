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
    message_index: int | None  # None: rendered by the template, not from a message
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


def _tool_sections(text: str, tools: list[Any]) -> list[tuple[int, int, str, str]]:
    """``(char_lo, char_hi, "tool_definitions", name)`` per tool schema in ``text``.

    Chat templates (Qwen3, Llama, Granite) render each tool with ``tojson`` into
    the system turn, where no message contains it. Each schema is serialised as
    the templates' ``tojson`` does (key order kept, no ASCII escaping) and
    declared only on a unique exact match; anything else stays a gap.
    """
    out: list[tuple[int, int, str, str]] = []
    for tool in tools:
        rendered = json.dumps(tool, ensure_ascii=False)
        found = text.find(rendered)
        if found < 0 or text.find(rendered, found + 1) >= 0:
            continue
        function = tool.get("function") if isinstance(tool, dict) else None
        name = (function or {}).get("name") if isinstance(function, dict) else None
        out.append((found, found + len(rendered), "tool_definitions", str(name or "tool")))
    return out


def _tools_block(text: str, without_tools: str) -> list[tuple[int, int, str, str]]:
    """The span ``text`` adds over the same prompt rendered without tools.

    Template-agnostic fallback for templates that format schemas their own way
    (Granite's ``tool_to_json`` macro): common prefix/suffix of the two renders.
    """
    prefix = 0
    limit = min(len(text), len(without_tools))
    while prefix < limit and text[prefix] == without_tools[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and text[len(text) - 1 - suffix] == without_tools[len(without_tools) - 1 - suffix]
    ):
        suffix += 1
    lo, hi = _strip_span(text, prefix, len(text) - suffix)
    return [(lo, hi, "tool_definitions", "tools")] if hi > lo else []


def render_without_tools(
    renderer: Any, messages: list[dict[str, Any]], template_kwargs: dict[str, Any]
) -> str | None:
    """``messages`` rendered with no tools, for :func:`_tools_block`; None without tools."""
    if not template_kwargs.get("tools"):
        return None
    kwargs = {k: v for k, v in template_kwargs.items() if k != "tools"}
    return renderer.render(messages, template_kwargs=kwargs)


def _tool_call_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    """``tool_call_id -> function name`` from the assistant messages' ``tool_calls``.

    OpenAI-format tool messages carry only the call id, so a tool result's
    range is labelled with the name of the call that produced it.
    """
    names: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = (call.get("function") or {}).get("name")
            if call.get("id") and name:
                names[str(call["id"])] = str(name)
    return names


def declare_ranges(
    messages: list[dict[str, Any]],
    encoded: Encoded,
    tools: list[Any] | None = None,
    without_tools: str | None = None,
) -> Declaration:
    """Labelled token ranges for every section of ``messages`` in ``encoded``.

    Each message's content is located in the rendered string by a forward
    search (templates may trim content or strip earlier thinking, so offsets
    are never assumed). A message that cannot be located is reported in
    ``unlocated_messages``, not dropped silently. ``tools`` are the schemas the
    template rendered; each found verbatim gets its own ``tool_definitions``
    range. When none is found verbatim, ``without_tools`` (the same request
    rendered with no tools) bounds the whole tools block as one range.
    """
    text = encoded.text
    cursor = 0
    sections: list[tuple[int, int, str, str, int | None]] = []
    unlocated: list[int] = []
    call_names = _tool_call_names(messages)
    for index, message in enumerate(messages):
        if message.get("role") == "tool" and not message.get("name"):
            name = call_names.get(str(message.get("tool_call_id") or ""))
            message = {**message, "name": name} if name else message
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
            sections.append((char_lo, char_hi, domain, label, index))
    tool_sections = _tool_sections(text, tools or [])
    if tools and not tool_sections and without_tools is not None:
        tool_sections = _tools_block(text, without_tools)
    for char_lo, char_hi, domain, label in tool_sections:
        sections.append((char_lo, char_hi, domain, label, None))
    ranges: list[DeclaredRange] = []
    for char_lo, char_hi, domain, label, index in sorted(sections, key=lambda s: s[0]):
        lo, hi = encoded.token_span(char_lo, char_hi)
        if ranges and lo < ranges[-1].hi:  # truncate overlaps, as the connector does
            lo = ranges[-1].hi
        if hi <= lo:
            continue
        ranges.append(DeclaredRange(lo, hi, domain, label, index, char_lo, char_hi))
    return Declaration(
        ranges=ranges, prompt_token_count=len(encoded.ids), unlocated_messages=unlocated
    )
