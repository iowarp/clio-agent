"""Tool calls over a single-prompt transport (Codex SDK, Claude Code SDK).

Those transports take ONE prompt string per turn and no native tools, so the engine
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
from collections.abc import Iterable, Sequence
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
# Calling tools

You can call the tools listed below. To call tools, write your message, then end it with
exactly one block that lists every call for this step:

```tool_calls
[{"name": "<tool name>", "arguments": {<arguments as a JSON object>}}]
```

All calls in one block run at the same time; you see all their results together. A message
that ends without a block is your final answer."""


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
        parts.append(f"{_TOOL_RULES}\n\n# Tools\n\n{listing}")
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


def split_reply(reply: str, *, call_prefix: str) -> ReplySplit:
    """Split a reply into visible text and tool calls (never repaired; see module doc)."""
    start = reply.rfind(FENCE)
    if start < 0:
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
        return ReplySplit(
            text=text,
            calls=[
                ToolCallPart(
                    id=f"{call_prefix}_0",
                    name=INVALID_TOOL_CALL,
                    input={"error": str(exc), "block": raw[:4000]},
                )
            ],
        )
    return ReplySplit(text=text, calls=parts)


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


class StreamSplitter:
    """Stream a reply's visible text live, holding back the tool-call block.

    ``push(chunk)`` returns the text that is safe to show now: everything before the
    fence, minus a tail that could still be the start of the fence. Once the fence is
    seen nothing more streams (the block is parsed at the end by :func:`split_reply`).
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._shown = 0
        self._stopped = False

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
