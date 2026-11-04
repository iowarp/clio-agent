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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "INVOKE_REMINDER",
    "INVOKE_RULES",
    "InvokeCalls",
    "parse_invoke_calls",
    "render_invoke",
    "text_tool_protocol",
]

_OPEN, _CLOSE = "<function_calls>", "</function_calls>"
_INVOKE = re.compile(r"""<invoke\s+name=(["'])(?P<name>[^"']+)\1\s*>(?P<body>.*?)</invoke>""", re.S)
#: A call's closing markup or a parameter without its <invoke name=...> opener.
_CUT = re.compile(r"""<parameter\s+name=|</invoke>|</function_calls>""")
#: The reason a cut call block is refused, in this protocol's own terms.
CUT_BLOCK = (
    "the call block was cut: it must open with <function_calls> and then "
    '<invoke name="tool name"> before its parameters; nothing ran, send the whole block again'
)
_PARAMETER = re.compile(
    r"""<parameter\s+name=(["'])(?P<name>[^"']+)\1\s*>(?P<value>.*?)</parameter>""", re.S
)

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
        "lm.text_tool_protocol", env="CLIO_TEXT_TOOL_PROTOCOL", default="json", cast=str
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


def parse_invoke_calls(reply: str, schemas: Mapping[str, Mapping[str, Any]]) -> InvokeCalls | None:
    """Read ``reply``'s ``<function_calls>`` block; ``None`` when it has none.

    ``schemas`` maps a tool name to its parameters schema; a parameter the schema does
    not type as a string must be JSON. Text before the block (and after it) is the
    visible text.
    """
    start = reply.find(_OPEN)
    if start < 0:
        bare = _INVOKE.search(reply)
        if bare is None:
            cut = _CUT.search(reply)
            if cut is None:
                return None
            # The call's opening lines are missing (seen on H4: the reply began at a
            # <parameter>): without the tool's name there is no call to run.
            return InvokeCalls(
                reply[: cut.start()].rstrip(), error=CUT_BLOCK, raw=reply[cut.start() :]
            )
        start = bare.start()  # an <invoke> without its container is still a call block
        end, close = reply.rfind("</invoke>") + len("</invoke>"), ""
    else:
        end = reply.find(_CLOSE, start)
        if end < 0:
            return InvokeCalls(reply[:start].rstrip(), error="the function_calls block was not "
                               "closed with </function_calls>", raw=reply[start:])  # fmt: skip
        close = _CLOSE
    block = reply[start : end + len(close)] if close else reply[start:end]
    after = reply[end + len(close) :].strip()
    text = "\n\n".join(t for t in (reply[:start].rstrip(), after) if t)
    if _OPEN in after or _INVOKE.search(after):
        return InvokeCalls(text, error="several function_calls blocks: put every call of this "
                           "step in one block", raw=reply[start:])  # fmt: skip
    calls: list[tuple[str, dict[str, Any]]] = []
    for invoke in _INVOKE.finditer(block):
        name = invoke["name"]
        properties = (schemas.get(name) or {}).get("properties") or {}
        arguments: dict[str, Any] = {}
        for parameter in _PARAMETER.finditer(invoke["body"]):
            key = parameter["name"]
            try:
                arguments[key] = _value(parameter["value"], properties.get(key) or {})
            except ValueError:
                return InvokeCalls(text, error=f"parameter {key} of {name} is not a string, so "
                                   "it must be written as JSON", raw=block)  # fmt: skip
        calls.append((name, arguments))
    if not calls:
        return InvokeCalls(text, error="the function_calls block holds no <invoke> call",
                           raw=block)  # fmt: skip
    return InvokeCalls(text, calls=calls, raw=block)


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
