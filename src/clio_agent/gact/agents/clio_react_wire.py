"""Keep model-internal wire tokens out of the replayed context (request only).

A Harmony-format model (gpt-oss family) served through vLLM can leak its own
control tokens -- ``<|start|>assistant<|channel|>analysis<|message|>`` ...
``<|end|>`` -- into ``reasoning_content`` or text. CLIO records the reply as it
came and replays it on the next step. A gateway that validates requests then
rejects the whole request (ALCF: HTTP 400 "Pass plain text in API fields, not
Harmony wire tags"), and every later step of the session fails the same way.

These tokens are tokenizer specials, never prose, so the request carries the
text without them. The recorded transcript is unchanged.
"""

from __future__ import annotations

import dataclasses
import re

from dspy.lm15 import Message, TextPart, ThinkingPart

_TAG = r"<\|(?:start|end|message|channel|constrain|call|return|endoftext|refusal)\|>"
#: A whole message header: ``<|start|>role[<|channel|>name][<|constrain|>fmt]<|message|>``.
_HEADER = re.compile(
    r"<\|start\|>[^<]{0,64}(?:<\|channel\|>[^<]{0,64})?(?:<\|constrain\|>[^<]{0,64})?<\|message\|>"
)
_ANY_TAG = re.compile(_TAG)


def strip_wire_tags(text: str) -> str:
    """``text`` without Harmony message headers and control tokens."""
    if "<|" not in text:
        return text
    return _ANY_TAG.sub("", _HEADER.sub("\n", text))


def without_wire_tags(messages: list[Message]) -> list[Message]:
    """Assistant text and thinking with wire tokens removed; other parts as they are."""
    out: list[Message] = []
    for message in messages:
        if message.role == "assistant" and any(
            isinstance(p, TextPart | ThinkingPart) and "<|" in p.text for p in message.parts
        ):
            parts = tuple(
                dataclasses.replace(p, text=strip_wire_tags(p.text))
                if isinstance(p, TextPart | ThinkingPart)
                else p
                for p in message.parts
            )
            message = dataclasses.replace(message, parts=parts)
        out.append(message)
    return out


__all__ = ["strip_wire_tags", "without_wire_tags"]
