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
import logging
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextvars import ContextVar
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

logger = logging.getLogger(__name__)

__all__ = [
    "CALLABLE_TOOLS",
    "INVALID_TOOL_CALL",
    "callable_tools",
    "FENCE",
    "TURN_REMINDER",
    "StreamSplitter",
    "render_messages",
    "render_system",
    "split_reply",
    "turn_reminder",
]

#: The fence that opens the tool-call block (closed by a line of three backticks).
FENCE = "```tool_calls"
#: The reserved tool name carrying a block that did not parse (its input: error, raw).
INVALID_TOOL_CALL = "clio_invalid_tool_calls"
#: Every tool the running agent may call, listed or not: a deferred tool that
#: search_tools loaded is callable without being listed. Recognizing a broken call block
#: (a call to one of these tools) needs the whole set, not only the listed tools.
CALLABLE_TOOLS: ContextVar[frozenset[str]] = ContextVar("clio_callable_tools", default=frozenset())


def callable_tools(listed: Collection[str]) -> frozenset[str]:
    """``listed`` plus every tool the running agent may call unlisted."""
    return frozenset(listed) | CALLABLE_TOOLS.get()


_TOOL_RULES = """\
# How you act

You act by calling the tools in the Available tools list below. They are your only way
to look anything up, read or change data, or run an analysis, so when the request needs
information you have not observed in this conversation, call the tools that get it
rather than answering from memory.

You call them by writing plain text: end your message with one block that lists every
call for this step:

```tool_calls
[{"name": "<tool name>", "arguments": {<arguments as a JSON object>}}]
```

For example, a step that looks two things up at once ends like this:

I'll check both sources.
```tool_calls
[{"name": "search_catalog", "arguments": {"query": "stations near Ridgecrest"}}, {"name": "read_notes", "arguments": {"path": "notes/site.md"}}]
```

(Those two names only illustrate the format; the names you call come from the Available
tools list.) All calls in one block run at the same time and their results come back
together in the next message, so put every call that does not need another call's
result into the same block, and make a call that needs one in a later step, once the
real result has arrived. Results come only from the host, so stop after the closing
fence. A message that ends without a block is your final answer."""

#: Added when the agent can load deferred tools: a loaded tool is callable unlisted.
_LOADED_TOOLS_RULE = (
    "Some tools are named in your instructions but not listed here; search_tools loads "
    "them. A tool whose schema a search_tools result returned is callable in the same way "
    "as a listed one, from the step after that result."
)

#: Appended to every user or tool-result turn the transport sends (never to the
#: system prompt): the call syntax, and a nudge to make each step as wide as it can be.
TURN_REMINDER = (
    "(To call tools, end your message with one fenced block: an opening line of three "
    "backticks followed by tool_calls, the JSON list of calls on the next line, then a "
    "closing line of three backticks; stop after it. First work out what you need next, "
    "then put every call that does not depend on another's result into this one block. "
    "If no calls are needed, answer normally.)"
)


def turn_reminder() -> str:
    """The call-syntax reminder of the configured protocol (``lm.text_tool_protocol``)."""
    from clio_agent.lm.engines import invoke_calls  # noqa: PLC0415

    return (
        invoke_calls.INVOKE_REMINDER
        if invoke_calls.text_tool_protocol() == "invoke"
        else TURN_REMINDER
    )


def render_system(system: str | None, tools: Sequence[Any]) -> str:
    """The tool rules and the tool list, then the system prompt.

    The tools come first, as in Anthropic's own request order (tools, system,
    messages): they are the same for every session of an agent, while the system
    prompt ends with per-session facts (the workspace, its write roots). Putting the
    stable text first keeps the provider's prompt-cache prefix shared across sessions;
    only the short per-session tail is new.
    """
    parts: list[str] = []
    functions = [t for t in tools if isinstance(t, FunctionTool)]
    if functions:
        listing = "\n".join(
            f"- {t.name}: {(t.description or '').strip()}\n  arguments schema: "
            f"{json.dumps(t.parameters, sort_keys=True)}"
            for t in functions
        )
        from clio_agent.lm.engines import invoke_calls  # noqa: PLC0415

        invoke = invoke_calls.text_tool_protocol() == "invoke"
        rules = invoke_calls.INVOKE_RULES if invoke else _TOOL_RULES
        if any(t.name == "search_tools" for t in functions):
            rules += f"\n\n{_LOADED_TOOLS_RULE}"
        parts.append(f"{rules}\n\n# Available tools\n\n{listing}")
    if system and system.strip():
        parts.append(system.strip())
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
        from clio_agent.lm.engines import invoke_calls  # noqa: PLC0415

        if not calls:
            block = ""
        elif invoke_calls.text_tool_protocol() == "invoke":
            block = "\n" + invoke_calls.render_invoke([(c["name"], c["arguments"]) for c in calls])
        else:
            block = f"\n{FENCE}\n{json.dumps(calls)}\n```"
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
    reply: str,
    *,
    call_prefix: str,
    available_tools: Collection[str] = (),
    schemas: Mapping[str, Mapping[str, Any]] | None = None,
) -> ReplySplit:
    """Split a reply into visible text and tool calls (never repaired; see module doc).

    Under the ``invoke`` protocol (``lm.text_tool_protocol``) a ``<function_calls>``
    block is the step's calls (:mod:`~clio_agent.lm.engines.invoke_calls`); a fenced
    JSON block is still read when there is none.
    """
    from clio_agent.lm.engines import invoke_calls  # noqa: PLC0415

    if invoke_calls.text_tool_protocol() == "invoke":
        found = invoke_calls.parse_invoke_calls(reply, schemas or {})
        if found is not None:
            return _invoke_split(found, call_prefix)
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
        if _element_call_block(reply, available_tools):
            return _invalid_reply(
                "",
                reply.strip(),
                "markup tags are not calls; a call needs a complete fenced ```tool_calls "
                "JSON list; no operation was submitted",
                call_prefix,
            )
        if _orphaned_call_block(reply, available_tools):
            return _invalid_reply(
                "", reply.strip(), "missing opening ```tool_calls fence", call_prefix
            )
        if _bracket_call_block(reply, available_tools):
            return _invalid_reply(
                "",
                reply.strip(),
                "[tool_calls] tags are not calls; a call needs a complete fenced "
                "```tool_calls JSON list; no operation was submitted",
                call_prefix,
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


def _invoke_split(found: Any, call_prefix: str) -> ReplySplit:
    """An invoke-protocol reply as a split. A fenced JSON block beside the calls is the
    same calls written twice when it is empty or equal to them (dropped, on the record);
    a different one is refused, since which to run is not the host's to guess."""
    if found.error:
        return _invalid_reply(found.text, found.raw, found.error, call_prefix)
    text, calls = found.text, [{"name": n, "arguments": a} for n, a in found.calls]
    fence = re.search(r"(?ms)^[ \t]*```tool_calls[ \t]*\r?\n(.*?)^[ \t]*```", text)
    if fence:
        try:
            listed = json.loads(fence[1])
        except ValueError:
            listed = None
        if listed not in ([], calls):
            return _invalid_reply(text, fence[0], "calls written both as a function_calls block "
                                  "and as a tool_calls block; write each call once, in one "
                                  "function_calls block", call_prefix)  # fmt: skip
        logger.info("invoke reply carried a duplicate tool_calls block; dropped (%d calls)",
                    len(listed or []))  # fmt: skip
        text = (text[: fence.start()] + text[fence.end() :]).strip()
    return ReplySplit(
        text=text, calls=[_call(c, f"{call_prefix}_{i}") for i, c in enumerate(calls)]
    )


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


def _element_call_block(reply: str, available_tools: Collection[str]) -> bool:
    """Reject a reply that is one markup element around a declared tool's name.

    Live, a typed child ended with ``<invoke_tool>submit</invoke_tool>`` and that markup
    became its answer. Whatever the tag, an element whose whole body is a declared
    tool's name is a call in a shape the host does not run; a name is never data.
    Attributes are not read, and markup inside prose, or around anything else, stays
    an answer.
    """
    element = re.fullmatch(
        r"\s*<(?P<tag>[A-Za-z_][\w:.-]*)(?:\s[^<>]*)?>(?P<body>.*?)</(?P=tag)>\s*",
        reply,
        re.DOTALL,
    )
    return bool(available_tools and element and element["body"].strip() in available_tools)


def _orphaned_call_block(reply: str, available_tools: Collection[str]) -> bool:
    """Recognize observed broken openers without guessing executable calls.

    Ordinary JSON answers and fenced examples stay answers. Only a whole call list
    or single call object for this request's tools followed by an unmatched closing fence is rejected,
    including live replies whose opening fence became ``_tool_calls`` or lost its
    backticks (``tool_calls``).
    """
    raw = reply.strip()
    if not available_tools or not raw.endswith("\n```") or "```" in raw[:-3]:
        return False
    body = raw[:-3]
    for opener in ("_tool_calls\n", "tool_calls\n"):
        body = body.removeprefix(opener)
    try:
        calls = json.loads(body)
    except ValueError:
        return False
    return _known_call_list(calls, available_tools) or _known_call_list([calls], available_tools)


def _bracket_call_block(reply: str, available_tools: Collection[str]) -> bool:
    """Reject a whole declared call closed by a ``[/tool_calls]`` tag (observed live).

    The reply is only the call (list or single object), optionally opened by
    ``[tool_calls]``; anything else, including an example inside prose, stays an answer.
    """
    match = re.fullmatch(
        r"\s*(?:\[tool_calls\]\s*)?(?P<body>.+?)\s*\[/tool_calls\]\s*", reply, re.DOTALL
    )
    if not available_tools or match is None:
        return False
    try:
        calls = json.loads(match["body"])
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


#: The fence's corrupted spelling on a line of its own (the opening backticks lost),
#: which models write next to a real block; protocol markup, never prose.
_FENCE_WORD_LINE = re.compile(r"(?m)^[ \t]*_tool_calls?[ \t]*\r?\n")
_FENCE_WORDS = ("_tool_calls", "_tool_call")


def _without_fence_words(text: str) -> tuple[str, int]:
    """``text`` without corrupted-fence lines; a last line that could still become one
    waits for its newline. Returns the text and how many lines were removed."""
    cleaned, removed = _FENCE_WORD_LINE.subn("", text)
    last = cleaned.rsplit("\n", 1)[-1].strip(" \t\r")
    if last and any(word.startswith(last) for word in _FENCE_WORDS):
        cleaned = cleaned[: len(cleaned) - len(cleaned.rsplit("\n", 1)[-1])]
    return cleaned, removed


class StreamSplitter:
    """Stream a reply's visible text live, holding back the tool-call block.

    ``push(chunk)`` returns the text that is safe to show now: everything before the
    fence, minus a tail that could still be the start of the fence. Once the fence is
    seen nothing more streams (the block is parsed at the end by :func:`split_reply`).
    A call the model also wrote as invoke markup in its prose is not shown: only the
    fenced block runs. ``dropped_invoke_markup`` counts those fragments, for the audit;
    ``dropped_fence_words`` counts lines that were only the fence's corrupted spelling.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._shown = 0
        self._stopped = False
        self.dropped_invoke_markup = 0
        self.dropped_fence_words = 0

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
        safe, self.dropped_fence_words = _without_fence_words(safe)
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
