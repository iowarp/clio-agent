"""Tool calls over a single-prompt transport (the Claude Code SDK engine).

Such a transport takes ONE prompt string per turn and no native tools, so the engine
carries the typed :class:`dspy.lm15.Request` across as text and brings tool calls back:

* :func:`render_system` -- the system prompt plus the tool list and the one rule the
  model follows to call tools: write your message, then end it with exactly one fenced
  ``tool_calls`` block (a JSON list of ``{"name", "arguments"}``). Calls in one block
  run in parallel; a message without a block is the final answer.
* :func:`render_messages` -- typed messages as text (user/developer text, the
  assistant's own earlier text + calls, tool results by call id); images (and, where
  the transport takes them, documents) are collected for its native media input.
* :func:`split_reply` -- the reply's visible text and its tool calls. A block that does
  not parse is NOT guessed at or repaired: it becomes one call to
  :data:`INVALID_TOOL_CALL`, whose observation is the parse error, so the model sees
  exactly what went wrong and fixes it on its next step.
* :class:`StreamSplitter` -- live text deltas that stop at the block, so the block
  never streams as visible text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from dspy.lm15 import (
    FunctionTool,
    ImagePart,
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)

__all__ = [
    "INVALID_TOOL_CALL",
    "FENCE",
    "TURN_REMINDER",
    "StreamSplitter",
    "render_messages",
    "render_system",
    "split_reply",
]

#: The fence that opens the tool-call block (closed by a line of three backticks).
FENCE = "```tool_calls"
#: The reserved tool name carrying a block that did not parse (its input: error, raw).
INVALID_TOOL_CALL = "clio_invalid_tool_calls"

_TOOL_RULES = """\
# How you act

Your only way to look anything up, read or change data, or run an analysis is to call the
tools in the Available tools list below -- there are no other capabilities. When the
request needs information you have not observed in this conversation, call the tools
that get it; do not answer from memory.

These tools are NOT function calls of your own runtime: you call them by writing plain
text in your reply. Write your message, then end it with exactly one block that lists
every call for this step:

```tool_calls
[{"name": "<tool name>", "arguments": {<arguments as a JSON object>}}]
```

For example, a step that looks two things up at once ends like this:

I'll check both sources.
```tool_calls
[{"name": "search_catalog", "arguments": {"query": "stations near Ridgecrest"}}, {"name": "read_notes", "arguments": {"path": "notes/site.md"}}]
```

(Those two names only illustrate the format -- call only names from the Available tools
list.) All calls in one block run at the same time; you see all their results together in
the next message. Stop your reply immediately after the closing fence. Do not author
new [tool results] sections or simulate later turns: only the host supplies actual
observations. If a call needs another call's output, emit only the prerequisite now,
then use its real result on the next step. Never invent a result or placeholder handle.
A message that ends without a block is your final answer."""

#: Appended to every user or tool-result turn the transport sends (never to the
#: system prompt), so the model is reminded where its tools are at each step.
TURN_REMINDER = (
    "(You act only through the Available tools. To call them, end your message with one "
    "complete fenced block: an opening line of three backticks followed by tool_calls, "
    "the JSON list on the next line, then a closing line of three backticks. Include BOTH "
    "fences; a JSON list alone or XML invoke/parameter tags are not calls. "
    "A JSON object wrapping a tool_calls list or XML <tool_calls> tags is not a call either. "
    "A single JSON call object also needs the complete fenced JSON list. "
    "Underscores are not tool-call fences. "
    "Stop after the closing fence and wait for host observations; do not simulate "
    "tool results or later turns. Dependent calls require the preceding real result. "
    "If no calls are needed, answer normally.)"
)


def render_system(system: str | None, tools: Sequence[Any]) -> str:
    """The system prompt followed by the tool rules and the tool list."""
    parts = [system.strip()] if system and system.strip() else []
    functions = [t for t in tools if isinstance(t, FunctionTool)]
    if functions:
        listing = "\n".join(
            f"- {t.name}: {(t.description or '').strip()}\n  arguments schema: "
            f"{json.dumps(t.parameters, sort_keys=True)}"
            for t in functions
        )
        parts.append(f"{_TOOL_RULES}\n\n# Available tools\n\n{listing}")
    return "\n\n".join(parts)


def render_messages(
    messages: Iterable[Message], *, media: tuple[type, ...] = (ImagePart,)
) -> tuple[str, list[Any]]:
    """Typed messages as one transcript text, plus the media parts they carry.

    ``media`` is the part kinds the transport takes natively beside the text (images by
    default; Claude Code also takes documents). Any other non-text part is a typed
    error, never dropped.
    """
    blocks: list[str] = []
    collected = _Media(media, [])
    for message in messages:
        text = _render_message(message, collected)
        if text:
            blocks.append(text)
    return "\n\n".join(blocks), collected.parts


@dataclass
class _Media:
    kinds: tuple[type, ...]
    parts: list[Any]


def _render_message(message: Message, media: _Media) -> str:
    if message.role == "tool":
        lines = ["[tool results]"]
        for part in message.parts:
            if isinstance(part, ToolResultPart):
                status = " (error)" if part.is_error else ""
                lines.append(
                    f"[{part.id} {part.name or ''}{status}]\n{_content_text(part.content, media)}"
                )
        return "\n".join(lines)
    if message.role == "assistant":
        text = "".join(p.text for p in message.parts if isinstance(p, TextPart))
        calls = [
            {"name": p.name, "arguments": p.input}
            for p in message.parts
            if isinstance(p, ToolCallPart)
        ]
        block = f"\n{FENCE}\n{json.dumps(calls)}\n```" if calls else ""
        return f"[assistant]\n{text}{block}".rstrip()
    body = _content_text(message.parts, media)
    return f"[{message.role}]\n{body}" if body else ""


def _content_text(parts: Iterable[Any], media: _Media) -> str:
    out: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            out.append(part.text)
        elif isinstance(part, media.kinds):
            media.parts.append(part)
            kind = "image" if isinstance(part, ImagePart) else "document"
            out.append(f"({kind} {len(media.parts)} attached)")
        elif isinstance(part, ThinkingPart):
            continue  # the transport keeps its own reasoning; never replayed as text
        else:
            raise TypeError(
                f"{type(part).__name__} cannot ride a single-prompt transport "
                "(it has no native input for it)"
            )
    return "\n".join(t for t in out if t)


@dataclass
class ReplySplit:
    """A reply's visible text and its tool calls (see :func:`split_reply`)."""

    text: str
    calls: list[ToolCallPart] = field(default_factory=list)


def split_reply(
    reply: str, *, call_prefix: str, available_tools: Collection[str] = ()
) -> ReplySplit:
    """Split a reply into visible text and tool calls (never repaired; see module doc)."""
    fences = list(re.finditer(r"(?m)^[ \t]*(?P<fence>```tool_calls)[ \t]*(?=\r?$)", reply))
    if len(fences) > 1:
        return _invalid_reply(
            reply[: fences[0].start()].rstrip(),
            reply[fences[0].start() :].strip(),
            "multiple tool-call blocks require one complete fenced JSON list containing "
            "every intended call; no operation was submitted",
            call_prefix,
        )
    start = fences[0].start("fence") if fences else reply.rfind(FENCE)
    if start < 0:
        if available_tools and re.fullmatch(
            r"\s*_tool_calls\r?\n.+\r?\n_tool_calls\s*", reply, re.DOTALL
        ):
            return _invalid_reply(
                "",
                reply.strip(),
                "broken tool-call delimiters require a complete fenced JSON call list; "
                "no operation was submitted",
                call_prefix,
            )
        if _xml_call_block(reply, available_tools):
            return _invalid_reply(
                "",
                reply.strip(),
                "XML tool-call fragments require a complete ```tool_calls JSON block",
                call_prefix,
            )
        if _orphaned_call_block(reply, available_tools):
            return _invalid_reply(
                "", reply.strip(), "missing opening ```tool_calls fence", call_prefix
            )
        if _bare_call_json(reply, available_tools):
            return _invalid_reply(
                "",
                reply.strip(),
                "tool-call JSON requires a complete fenced ```tool_calls JSON list; "
                "no operation was submitted",
                call_prefix,
            )
        return ReplySplit(text=reply.strip())
    body = reply[start + len(FENCE) :]
    end = body.find("```")
    raw = (body[:end] if end >= 0 else body).strip()
    # Anything the model wrote after the block is still its visible text.
    after = body[end + 3 :].strip() if end >= 0 else ""
    text = "\n\n".join(t for t in (reply[:start].rstrip(), after) if t)
    try:
        calls = json.loads(raw)
        if not isinstance(calls, list) or not calls:
            raise ValueError("the block must be a non-empty JSON list")
        parts = [_call(call, f"{call_prefix}_{i}") for i, call in enumerate(calls)]
    except (ValueError, TypeError) as exc:
        return _invalid_reply(text, raw, str(exc), call_prefix)
    return ReplySplit(text=text, calls=parts)


def _xml_call_block(reply: str, available_tools: Collection[str]) -> bool:
    """Reject observed native call shapes without inferring or executing their arguments."""
    if not available_tools:
        return False
    container = re.fullmatch(r"\s*<tool_calls>\s*(.*?)\s*</tool_calls>\s*", reply, re.DOTALL)
    if container:
        try:
            return _known_call_list(json.loads(container[1]), available_tools)
        except ValueError:
            return False
    parameter_calls = re.fullmatch(
        r"""\s*<parameter\s+name=(["'])tool_calls\1\s*>\s*(.*?)\s*</parameter>\s*""",
        reply,
        re.DOTALL,
    )
    if parameter_calls:
        try:
            return _known_call_list(json.loads(parameter_calls[2]), available_tools)
        except ValueError:
            return False
    match = re.match(r"""\s*<invoke\s+name=(["'])([^"']+)\1\s*>""", reply)
    if match:
        return match[2] in available_tools and "</invoke>" in reply[match.end() :]
    # A stopped Haiku parent resumed with parameter tags and orphaned invoke
    # closers. There is no tool identity to resolve: reject this malformed shape
    # through the existing bounded error, never fabricate a callable operation.
    parameter = re.match(r"""\s*<parameter\s+name=(["'])[^"']+\1\s*>""", reply)
    return bool(
        parameter
        and "</parameter>" in reply[parameter.end() :]
        and "</invoke>" in reply[parameter.end() :]
    )


def _orphaned_call_block(reply: str, available_tools: Collection[str]) -> bool:
    """Recognize observed broken openers without guessing executable calls.

    Ordinary JSON answers and fenced examples stay answers. Only a whole call list
    or single call object for this request's tools followed by an unmatched closing fence is rejected,
    including the live reply whose opening fence became ``_tool_calls``.
    """
    raw = reply.strip()
    if not available_tools or not raw.endswith("\n```") or "```" in raw[:-3]:
        return False
    body = raw[:-3]
    if body.startswith("_tool_calls\n"):
        body = body.removeprefix("_tool_calls\n")
    try:
        calls = json.loads(body)
    except ValueError:
        return False
    return _known_call_list(calls, available_tools) or _known_call_list([calls], available_tools)


def _bare_call_json(reply: str, available_tools: Collection[str]) -> bool:
    """Reject a whole declared call or reserved envelope; never reinterpret examples."""
    if not available_tools or not reply.lstrip().startswith("{"):
        return False
    try:
        envelope = json.loads(reply)
    except ValueError:
        return False
    if not isinstance(envelope, dict):
        return False
    return _known_call_list([envelope], available_tools) or (
        set(envelope) == {"tool_calls"}
        and _known_call_list(envelope["tool_calls"], available_tools)
    )


def _known_call_list(calls: Any, available_tools: Collection[str]) -> bool:
    """Recognize only exact call objects for declared tools, without resolving arguments."""
    return (
        isinstance(calls, list)
        and bool(calls)
        and all(
            isinstance(call, dict)
            and set(call) == {"name", "arguments"}
            and isinstance(call["name"], str)
            and call["name"] in available_tools
            and isinstance(call["arguments"], dict)
            for call in calls
        )
    )


def _invalid_reply(text: str, raw: str, error: str, call_prefix: str) -> ReplySplit:
    """Return a bounded protocol-error observation; never execute malformed content."""
    return ReplySplit(
        text=text,
        calls=[
            ToolCallPart(
                id=f"{call_prefix}_0",
                name=INVALID_TOOL_CALL,
                input={"error": error, "block": raw[:4000]},
            )
        ],
    )


def _call(call: Any, call_id: str) -> ToolCallPart:
    if not isinstance(call, dict):
        raise TypeError(f"each call must be an object, got {type(call).__name__}")
    name = call.get("name")
    arguments = call.get("arguments", {})
    if not isinstance(name, str) or not name:
        raise ValueError("each call needs a non-empty string 'name'")
    if not isinstance(arguments, dict):
        raise TypeError(f"'arguments' of {name} must be a JSON object")
    return ToolCallPart(id=call_id, name=name, input=arguments)


#: A call written as the model's native invoke markup in its prose. Only the fenced block
#: is ever executed, so the markup is a duplicate the person should not read as text.
_INVOKE_MARKUP = re.compile(
    r"""(?:<function_calls>\s*)?<invoke\s+name=(["'])[^"']*\1\s*>.*?</invoke>"""
    r"""(?:\s*</function_calls>)?""",
    re.DOTALL,
)
_INVOKE_OPENER = re.compile(r"""<function_calls>|<invoke\s+name=""")
_INVOKE_OPENERS = ("<function_calls>", "<invoke name=")


def _without_invoke_markup(text: str) -> tuple[str, int]:
    """``text`` without complete invoke fragments, cut before an unfinished one (or a tail
    that could start one); returns the text and how many fragments were removed."""

    cleaned, removed = _INVOKE_MARKUP.subn("", text)
    if opener := _INVOKE_OPENER.search(cleaned):
        return cleaned[: opener.start()], removed
    for n in range(min(max(map(len, _INVOKE_OPENERS)) - 1, len(cleaned)), 0, -1):
        if any(o.startswith(cleaned[-n:]) for o in _INVOKE_OPENERS):
            return cleaned[:-n], removed
    return cleaned, removed


class StreamSplitter:
    """Stream a reply's visible text live, holding back the tool-call block.

    ``push(chunk)`` returns the text that is safe to show now: everything before the
    fence, minus a tail that could still be the start of the fence. Once the fence is
    seen nothing more streams (the block is parsed at the end by :func:`split_reply`).
    A call the model also wrote as invoke markup in its prose is not shown: only the
    fenced block runs. ``dropped_invoke_markup`` counts those fragments, for the audit.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._shown = 0
        self._stopped = False
        self.dropped_invoke_markup = 0

    def push(self, chunk: str) -> str:
        """Add ``chunk``; return the newly showable text (possibly empty)."""
        if self._stopped:
            return ""
        self._buffer += chunk
        fence_at = self._buffer.find(FENCE)
        if fence_at >= 0:
            self._stopped = True
            safe = self._buffer[:fence_at]
        else:
            safe = self._buffer[: len(self._buffer) - _partial_fence_len(self._buffer)]
        safe, self.dropped_invoke_markup = _without_invoke_markup(safe)
        # Trailing newlines wait for what follows: before the fence they are not text.
        safe = safe.rstrip("\n")
        out = safe[self._shown :]
        self._shown = max(self._shown, len(safe))
        return out


def _partial_fence_len(text: str) -> int:
    """Length of the longest suffix of ``text`` that is a proper prefix of the fence."""
    for n in range(min(len(FENCE) - 1, len(text)), 0, -1):
        if FENCE.startswith(text[-n:]):
            return n
    return 0
