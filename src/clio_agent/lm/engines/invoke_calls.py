"""Tool calls written as invoke markup: the text protocol in the model's own call format.

The single-prompt transport (:mod:`clio_agent.lm.engines.text_tools`) has no native
tools, so calls come back as text. Its first protocol is a fenced ```tool_calls JSON
list. On X2 (2026-11-04) Claude wrote 2-8 calls per run in its trained call markup
first (``<invoke name=...><parameter name=...>...</parameter></invoke>``, a whole script
included), then the same call again as the JSON block, or an empty ``[]`` block as if
the markup had made the call. That markup was hidden from the visible text and never
run: 1-41% of a run's visible output (median about a third) was a duplicate, and
about two calls a run were unreadable blocks, each a wasted model step.

This protocol takes that markup as the call. A string parameter is its raw text (a
script is written once, unescaped); any other parameter is JSON, read against the
tool's schema. Nothing is guessed: a value that is not the JSON its schema asks for,
an unclosed block, or a second block is refused with the reason, as for the JSON
block. Selected by ``lm.text_tool_protocol`` (``json`` or ``invoke``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "INVOKE_REMINDER",
    "INVOKE_RULES",
    "InvokeCalls",
    "parse_invoke_calls",
    "plain_call_tags",
    "render_invoke",
    "text_tool_protocol",
]

_OPEN, _CLOSE = "<function_calls>", "</function_calls>"
_INVOKE = re.compile(r"""<invoke\s+name=(["'])(?P<name>[^"']+)\1\s*>(?P<body>.*?)</invoke>""", re.S)
#: A fenced code block (``` ... ```): markup inside it is an example, not a call.
_FENCED = re.compile(r"```.*?(?:```|\Z)", re.S)
#: An <invoke name=...> opener (closed or not).
_INVOKE_OPEN = re.compile(r"""<invoke\s+name=(["'])(?P<name>[^"']+)\1\s*>""")
#: A call's closing markup or a parameter without its <invoke name=...> opener.
_CUT = re.compile(r"""<parameter\s+name=|</invoke>|</function_calls>""")
#: A line that begins with the container's word but is not its tag: what is left of a
#: call block whose markup was lost (``function_calls="" invoke name="run_code">``,
#: ``function_calls: invoke run_code``), or the ``<invoke_tool_placeholder>`` tags the
#: service substitutes for call markup written as text, or an invented ``<invoke:NAME>``
#: opener models write beside a real block. Never prose and never a call.
_RESIDUE = re.compile(
    r"""(?m)^[ \t]*(?:<?/?function_calls(?!>)\b|</?invoke_tool_placeholder>"""
    r"""|</?invoke:[\w.-]+>).*$"""
)
#: A call tag written with an XML namespace prefix (``<ns:invoke name=...>``,
#: ``<ns:parameter ...>``, their closers): the model's trained call markup in its
#: namespaced spelling. Live (sonnet, 2026-11-12) a step began with a namespaced
#: ``<parameter>`` that neither the stream splitter nor this parser knew, so the cut
#: call streamed and was stored as the step's visible text.
_NAMESPACED_TAG = re.compile(r"<(/?)[A-Za-z_][\w.-]*:(function_calls|invoke|parameter)(?=[\s>/])")
#: The reason a cut call block is refused, in this protocol's own terms.
CUT_BLOCK = (
    "the call block was cut: it must open with <function_calls> and then "
    '<invoke name="tool name"> before its parameters; nothing ran, send the whole block again'
)
_PARAMETER = re.compile(
    r"""<parameter\s+name=(["'])(?P<name>[^"']+)\1\s*>(?P<value>.*?)</parameter>""", re.S
)
_GAP = re.compile(r"\s*")
#: The block's end: stray closing tags haiku adds (``</tool>``, a second ``</invoke>``),
#: then the container's own.
_BLOCK_CLOSE = re.compile(r"(?:\s*</(?:invoke|tool)>)*(?:\s*</function_calls>)?")

INVOKE_RULES = """\
# How you act

You act by calling the tools in the Available tools list below. They are your only way
to look anything up, read or change data, or run an analysis, so when the request needs
information you have not observed in this conversation, call the tools that get it
rather than answering from memory.

You call them by ending your message with one function_calls block that holds every
call for this step:

<function_calls>
<invoke name="<tool name>">
<parameter name="<argument name>">value</parameter>
</invoke>
</function_calls>

A string argument is written as it is, on as many lines as it needs, with nothing
escaped; any other argument (a number, true or false, a list or an object) is written
as JSON. All calls in one block run at the same time and their results come back
together in the next message, so put every call that does not need another call's
result into the same block, and make a call that needs one in a later step, once the
real result has arrived. Results come only from the host, so stop after
</function_calls>. A message that ends without a block is your final answer."""

#: Appended to every user or tool-result turn (never to the system prompt).
INVOKE_REMINDER = (
    "(To call tools, end your message with one <function_calls> block of <invoke> calls "
    "and stop after </function_calls>. First work out what you need next, then put every "
    "call that does not depend on another's result into this one block. If no calls are "
    "needed, answer normally.)"
)


def text_tool_protocol() -> str:
    """The configured call protocol: ``json`` (the fenced block) or ``invoke``."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "lm.text_tool_protocol", env="CLIO_TEXT_TOOL_PROTOCOL", default="invoke", cast=str
    )
    if value not in ("json", "invoke"):
        raise ValueError(f"lm.text_tool_protocol must be json or invoke, not {value!r}")
    return value


@dataclass
class InvokeCalls:
    """A reply read under the invoke protocol: visible text, calls, or why it was refused."""

    text: str
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    error: str = ""
    raw: str = ""
    #: What the model wrote on past its first call block (see :func:`call_block_end`).
    overrun: str = ""


def plain_call_tags(text: str) -> str:
    """``text`` with namespaced call tags in their plain spelling (``<invoke``,
    ``</parameter>``), so they are read, and hidden, as the call markup they are."""
    return _NAMESPACED_TAG.sub(r"<\1\2", text)


def _accepts_string(schema: Mapping[str, Any]) -> bool:
    kind = schema.get("type")
    if kind is None:
        options = schema.get("anyOf") or schema.get("oneOf") or []
        return not options or any(_accepts_string(o) for o in options if isinstance(o, Mapping))
    return kind == "string" or (isinstance(kind, list) and "string" in kind)


def _value(raw: str, schema: Mapping[str, Any]) -> Any:
    text = raw[1:] if raw.startswith("\n") else raw
    text = text[:-1] if text.endswith("\n") else text
    if _accepts_string(schema):
        return text
    return json.loads(text)


def parse_invoke_calls(
    reply: str, schemas: Mapping[str, Mapping[str, Any]], known: Collection[str] = ()
) -> InvokeCalls | None:
    """Read ``reply``'s calls; ``None`` when it has none.

    The calls are its complete ``<invoke name=...>...</invoke>`` elements, in order. The
    ``<function_calls>`` container is decoration: across the protocol A/B's runs it was
    missing before 18 of 57 calls (the reply began at ``<invoke``) and once trailed the
    calls on its own, so it is stripped from the visible text and never decides what
    runs. A parameter or closing tag with no ``<invoke name=...>`` (the tool's name lost:
    1 of 57) or an ``<invoke>`` never closed is refused, since there is no whole call to
    run. ``schemas`` maps a tool name to its parameters schema; a parameter the schema
    does not type as a string must be JSON.

    Markup that is data is never a call: an ``<invoke>`` inside a fenced code block is
    an example, and a reply whose invokes name none of the ``known`` tools (or with no
    tools at all) is an answer that happens to contain markup.

    The step is the reply up to the end of its first call block (:func:`call_block_end`):
    the transport has no stop sequence, and what the model wrote past the block was
    written before any result existed, so it is returned as ``overrun``, never run or
    shown.
    """
    if not known:
        return None
    reply = plain_call_tags(reply)
    overrun = ""
    if (cut := call_block_end(reply, known)) is not None:
        reply, overrun = reply[:cut], reply[cut:]
    found = _parse(reply, schemas, known)
    if found is not None:
        found.overrun = overrun
    return found


def call_block_end(reply: str, known: Collection[str]) -> int | None:
    """Where ``reply``'s first call block ends, when the model wrote on past it.

    The block is the run of ``<invoke>`` elements that begins the reply's calls, apart
    only by blank space, and its closing ``</function_calls>``. haiku wrote on past it
    (V, 2026-11-10): prose about results it had not received, a ``[tool results]``
    block of its own in the host's format, then more blocks whose calls depended on the
    invented results. ``None`` when nothing but blank space follows the block, or the
    reply has no call of a ``known`` tool.
    """
    fences = [m.span() for m in _FENCED.finditer(reply)]
    invokes = [m for m in _INVOKE.finditer(reply) if not any(a <= m.start() < b for a, b in fences)]
    if not invokes or not any(m["name"] in known for m in invokes):
        return None
    end = invokes[0].end()
    for invoke in invokes[1:]:
        if invoke.start() != _GAP.match(reply, end).end():  # type: ignore[union-attr]
            break
        end = invoke.end()
    if close := _BLOCK_CLOSE.match(reply, end):
        end = close.end()
    return end if reply[end:].strip() else None


#: What may still follow a call block while it streams: the next ``<invoke`` being
#: written, or the block's closing tags (whole ones are already part of the block).
_BLOCK_CONTINUATIONS = (
    "<invoke ",
    "<invoke\n",
    "<invoke\t",
    "</invoke>",
    "</tool>",
    "</function_calls>",
)


def writing_past_block(reply: str, known: Collection[str]) -> bool:
    """Whether a reply still streaming has gone on past its first call block.

    Everything past the block is never the step's (:func:`call_block_end`): invented
    results, prose about them, later blocks. A stream can stop there and lose nothing.
    Text that could still be the block's next ``<invoke`` or its closing tag is not past
    it yet, so a block of several calls is never cut while it is being written.
    """
    reply = plain_call_tags(reply)
    end = call_block_end(reply, known)
    if end is None:
        return False
    past = reply[end:].lstrip()
    return not any(c.startswith(past) or past.startswith(c) for c in _BLOCK_CONTINUATIONS)


def _parse(
    reply: str, schemas: Mapping[str, Mapping[str, Any]], known: Collection[str]
) -> InvokeCalls | None:
    """:func:`parse_invoke_calls` on a reply that ends at its call block."""
    fences = [m.span() for m in _FENCED.finditer(reply)]
    invokes = [m for m in _INVOKE.finditer(reply) if not any(a <= m.start() < b for a, b in fences)]
    if invokes and not any(m["name"] in known for m in invokes):
        return None
    if not invokes:
        outside = _FENCED.sub("", reply)  # markup in a fenced block is an example
        opened = _INVOKE_OPEN.search(outside)
        if opened is None and _CUT.search(outside) is None:
            if residue := _RESIDUE.search(outside):
                return InvokeCalls(_visible(outside[: residue.start()]), error=CUT_BLOCK,
                                   raw=outside[residue.start():])  # fmt: skip
            return None
        if opened is not None and opened["name"] not in known:
            return None
        start = (opened or _CUT.search(outside)).start()  # type: ignore[union-attr]
        reason = "an <invoke> was not closed with </invoke>; nothing ran, send it again whole"
        return InvokeCalls(_visible(outside[:start]), error=reason if opened else CUT_BLOCK,
                           raw=outside[start:])  # fmt: skip
    pieces, last = [], 0
    for invoke in invokes:
        pieces.append(reply[last : invoke.start()])
        last = invoke.end()
    pieces.append(reply[last:])
    text = _visible("".join(pieces))
    raw = reply[invokes[0].start() : invokes[-1].end()]
    if _INVOKE_OPEN.search(_FENCED.sub("", text)) or _PARAMETER.search(_FENCED.sub("", text)):
        return InvokeCalls(text, error=CUT_BLOCK, raw=raw)  # a whole call beside a broken one
    calls: list[tuple[str, dict[str, Any]]] = []
    for invoke in invokes:
        name = invoke["name"]
        properties = (schemas.get(name) or {}).get("properties") or {}
        arguments: dict[str, Any] = {}
        for parameter in _PARAMETER.finditer(invoke["body"]):
            key = parameter["name"]
            try:
                arguments[key] = _value(parameter["value"], properties.get(key) or {})
            except ValueError:
                return InvokeCalls(text, error=f"parameter {key} of {name} is not a string, so "
                                   "it must be written as JSON", raw=raw)  # fmt: skip
        calls.append((name, arguments))
    return InvokeCalls(text, calls=calls, raw=raw)


def _visible(text: str) -> str:
    """``text`` without the container tags or their residue, its blank runs closed up."""
    cleaned = _RESIDUE.sub("", text.replace(_OPEN, "").replace(_CLOSE, ""))
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def render_invoke(calls: list[tuple[str, Mapping[str, Any]]]) -> str:
    """Calls as a ``<function_calls>`` block (the assistant's own earlier calls)."""
    lines = [_OPEN]
    for name, arguments in calls:
        lines.append(f'<invoke name="{name}">')
        for key, value in arguments.items():
            shown = value if isinstance(value, str) else json.dumps(value)
            lines.append(f'<parameter name="{key}">{shown}</parameter>')
        lines.append("</invoke>")
    lines.append(_CLOSE)
    return "\n".join(lines)
