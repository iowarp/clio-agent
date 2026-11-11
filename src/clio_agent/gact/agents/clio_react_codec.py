"""The step codec: typed ``dspy.lm15`` parts to plane content and back, and the fold.

Moved out of :mod:`clio_agent.gact.agents.clio_react_record` (which re-exports these
names) so the recorder stays under the file-size cap. :func:`fold_steps` groups a
scope's ordered live segments into typed messages, one assistant and one tool message
per step; anything it cannot fold exactly is a :class:`ContextFoldError`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dspy.lm15 import (
    ContinuationState,
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)

from clio_agent.gact.agents.context_errors import ContextFoldError
from clio_agent.gact.agents.recorded_media import result_part

__all__ = [
    "fold_steps",
    "thinking_from_record",
    "thinking_to_record",
    "user_from_record",
    "user_to_record",
]

_FOLDED_KINDS = frozenset({"user", "thought", "tool_call", "observation", "summary"})
# clio-core record kinds that are never the agent's context (skipped by declaration).
_NON_CONTEXT_KINDS = frozenset(
    {
        "answer",
        "semantic_event",
        "ws_op",
        "step_open",
        "message_part",
        "state_merge",
        "recipient_input",
    }
)


# --------------------------------------------------------------------------- #
# The step codec: typed parts <-> plane content (CLIO-owned, JSON)            #
# --------------------------------------------------------------------------- #
def thinking_to_record(part: ThinkingPart, source: str = "") -> dict[str, Any]:
    """A thinking part as plane content, provider continuation state kept byte-exact.

    ``source`` (``provider_field`` / ``inline_tag``, see ``gact.reasoning_extract``) is
    kept when known; :func:`thinking_from_record` ignores it.
    """
    return {
        "text": part.text,
        "continuation": [
            {"provider": c.provider, "kind": c.kind, "data": c.data} for c in part.continuation
        ],
        **({"source": source} if source else {}),
    }


def thinking_from_record(record: Mapping[str, Any]) -> ThinkingPart:
    """Rebuild a thinking part from :func:`thinking_to_record`'s content."""
    return ThinkingPart(
        text=str(record.get("text") or ""),
        continuation=tuple(
            ContinuationState(provider=c["provider"], kind=c["kind"], data=c.get("data") or {})
            for c in record.get("continuation") or []
            if isinstance(c, Mapping)
        ),
    )


def user_to_record(message: Message) -> dict[str, Any]:
    """A user message as plane content: its text and its media, byte-exact."""
    text = "".join(p.text for p in message.parts if isinstance(p, TextPart))
    media = [
        {
            "type": "image" if isinstance(p, ImagePart) else "document",
            "media_type": p.media_type,
            "data": p.data,
        }
        for p in message.parts
        if isinstance(p, ImagePart | DocumentPart)
    ]
    return {"text": text, "media": media} if media else {"text": text}


def user_from_record(record: Mapping[str, Any]) -> Message:
    """Rebuild a user message from :func:`user_to_record`'s content.

    A CLIO addition (``actor: algorithm``) is headed with its source, so the model
    can tell it from what the user wrote.
    """
    text = _text(record.get("text"))
    if record.get("actor") == "algorithm":  # CLIO's addition; a steer is the user's own
        text = f"[clio: {_text(record.get('source'))}]\n{text}"
    parts: list[Any] = [TextPart(text=text)]
    for item in record.get("media") or []:
        if not isinstance(item, Mapping):
            continue
        cls = ImagePart if item.get("type") == "image" else DocumentPart
        parts.append(cls(data=str(item.get("data") or ""), media_type=str(item["media_type"])))
    return Message(role="user", parts=tuple(parts))


def fold_steps(segments: Sequence[Any]) -> list[Message]:
    """Group ordered live segments into typed messages, one assistant + tool pair per step.

    A ``user`` segment is the user message it recorded. A ``thought`` opens a step; its
    ``tool_call`` / ``observation`` segments attach to it, each result matched to its call
    by ``call_id``; a call with no step open starts its own. A ``summary`` becomes a user
    message carrying its text, so compacted content still reaches the model. Record kinds
    that are never agent context are skipped by declaration; anything else that cannot be
    folded exactly is a :class:`ContextFoldError`.
    """
    messages: list[Message] = []
    step: _StepFold | None = None

    def close(seg: Any) -> None:
        nonlocal step
        if step is not None:
            messages.extend(step.messages(seg))
            step = None

    for seg in segments:
        kind = getattr(seg, "kind", "")
        if kind in _NON_CONTEXT_KINDS:
            continue
        if kind not in _FOLDED_KINDS:
            raise ContextFoldError("unmodelled_kind", seg)
        content = getattr(seg, "content", None)
        if not isinstance(content, Mapping):
            raise ContextFoldError("malformed_content", seg)
        if kind == "thought":
            close(seg)
            step = _StepFold(content, seg)
        elif kind == "tool_call":
            if step is None:  # a call with no thought before it (an edit put it there)
                step = _StepFold({}, seg)
            step.add_call(content, seg)
        elif kind == "observation":
            if step is None:
                raise ContextFoldError("orphan_observation", seg, "no step is open")
            step.add_result(content, seg)
        elif kind == "user":
            close(seg)
            messages.append(user_from_record(content))
        else:  # summary
            close(seg)
            text = _text(content.get("text"))
            if not text:
                raise ContextFoldError("empty_summary", seg)
            messages.append(Message.user(f"[earlier context]\n{text}"))
    close(None)
    return messages


class _StepFold:
    """Accumulates one step's thinking, text, calls and results while folding."""

    def __init__(self, content: Mapping[str, Any], seg: Any) -> None:
        self.text = _text(content.get("text"))
        thinking = content.get("thinking") or []
        if not isinstance(thinking, list) or not all(isinstance(t, Mapping) for t in thinking):
            raise ContextFoldError("malformed_thinking", seg)
        try:
            self.thinking = [thinking_from_record(t) for t in thinking]
        except (KeyError, TypeError) as exc:
            raise ContextFoldError("malformed_thinking", seg, str(exc)) from exc
        self.calls: list[ToolCallPart] = []
        self.results: dict[str, ToolResultPart] = {}

    def add_call(self, content: Mapping[str, Any], seg: Any) -> None:
        call_id, name, args = content.get("id"), content.get("name"), content.get("args", {})
        if not call_id or not name or not isinstance(args, Mapping):
            raise ContextFoldError("malformed_call", seg)
        self.calls.append(ToolCallPart(id=str(call_id), name=str(name), input=dict(args)))

    def add_result(self, content: Mapping[str, Any], seg: Any) -> None:
        call_id = str(content.get("call_id") or "")
        call = next((c for c in self.calls if c.id == call_id), None)
        if call is None or call_id in self.results:
            raise ContextFoldError("orphan_observation", seg, f"no open call {call_id!r}")
        self.results[call_id] = result_part(
            call_id,
            call.name,
            content.get("text", ""),
            bool(content.get("is_error")),
            call_ref=str(content.get("call_ref") or ""),
        )

    def messages(self, next_seg: Any) -> list[Message]:
        unanswered = [c.id for c in self.calls if c.id not in self.results]
        if unanswered:
            raise ContextFoldError("unanswered_call", next_seg, ", ".join(unanswered))
        parts: list[Any] = [*self.thinking]
        if self.text or not (self.thinking or self.calls):
            parts.append(TextPart(text=self.text))
        parts.extend(self.calls)
        out = [Message(role="assistant", parts=tuple(parts))]
        if self.calls:
            out.append(Message(role="tool", parts=tuple(self.results[c.id] for c in self.calls)))
        return out


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value if value is not None else "")
