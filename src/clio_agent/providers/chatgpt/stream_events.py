"""Codex Responses-API event -> normalized ChatGPT event mapping (A.5).

The SSE and WebSocket transports receive identical JSON event payloads (only
the wire framing differs -- ``data:`` lines vs. one JSON text frame). This
module is the ONE place that classifies a raw event dict into the small
internal vocabulary :mod:`clio_agent.providers.chatgpt.litellm_adapter`
consumes, so a fix to event mapping lands once for both transports.

Tool-call arguments are accumulated internally and surfaced as ONE
:class:`ToolCallDone` per call, once ``response.output_item.done`` fires,
rather than forwarded as raw per-token argument deltas -- DSPy's ReAct loop
only needs the finished call, and this keeps the adapter's aggregation simple
and robust. Answer text and reasoning-summary text ARE forwarded incrementally
(:class:`TextDelta`/:class:`ReasoningDelta`) since that is what makes
perceived latency track real TTFT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

__all__ = [
    "Completed",
    "Failed",
    "ResponseEventParser",
    "StreamEvent",
    "TextDelta",
    "ToolCallDone",
]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDone:
    call_id: str
    name: str
    arguments: str
    index: int


@dataclass(frozen=True)
class Completed:
    response_id: str
    output_items: list[dict[str, Any]]
    usage: dict[str, Any]


@dataclass(frozen=True)
class Failed:
    code: str | None
    message: str


StreamEvent = Union[TextDelta, ReasoningDelta, ToolCallDone, Completed, Failed]

#: Event types that mean "the stream is over" (A.5: normalize all three).
_TERMINAL_EVENT_TYPES = frozenset({"response.completed", "response.done", "response.incomplete"})


class ResponseEventParser:
    """Stateful mapper from one turn's raw Responses events to :data:`StreamEvent`.

    One instance per turn (HTTP or WebSocket) -- never shared or reused across
    turns. Accumulates function-call arguments and the running output-item
    list as the stream progresses.
    """

    def __init__(self) -> None:
        self._function_calls: dict[str, dict[str, Any]] = {}
        self._output_items: list[dict[str, Any]] = []
        self._response_id = ""
        self._usage: dict[str, Any] = {}
        self._completed = False

    @property
    def completed(self) -> bool:
        """Whether a terminal event (completed/done/incomplete/failed/error) arrived."""

        return self._completed

    @property
    def response_id(self) -> str:
        return self._response_id

    @property
    def output_items(self) -> list[dict[str, Any]]:
        return list(self._output_items)

    def feed(self, event: dict[str, Any]) -> list[StreamEvent]:
        """Consume one raw event dict; return zero or more normalized events."""

        event_type = str(event.get("type") or "")
        if event_type == "response.output_text.delta":
            return self._text_delta(event)
        if event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            return self._reasoning_delta(event)
        if event_type == "response.output_item.added":
            return self._item_added(event)
        if event_type == "response.function_call_arguments.delta":
            return self._arguments_delta(event)
        if event_type == "response.output_item.done":
            return self._item_done(event)
        if event_type in _TERMINAL_EVENT_TYPES:
            return self._terminal(event)
        if event_type == "response.failed":
            return self._failed(event)
        if event_type == "error":
            return self._bare_error(event)
        return []

    def _text_delta(self, event: dict[str, Any]) -> list[StreamEvent]:
        delta = str(event.get("delta") or "")
        return [TextDelta(delta)] if delta else []

    def _reasoning_delta(self, event: dict[str, Any]) -> list[StreamEvent]:
        delta = str(event.get("delta") or "")
        return [ReasoningDelta(delta)] if delta else []

    def _item_added(self, event: dict[str, Any]) -> list[StreamEvent]:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            item_id = str(event.get("item_id") or item.get("id") or "")
            self._function_calls[item_id] = {
                "call_id": str(item.get("call_id") or ""),
                "name": str(item.get("name") or ""),
                "arguments": str(item.get("arguments") or ""),
                "index": int(event.get("output_index") or 0),
            }
        return []

    def _arguments_delta(self, event: dict[str, Any]) -> list[StreamEvent]:
        entry = self._function_calls.get(str(event.get("item_id") or ""))
        if entry is not None:
            entry["arguments"] += str(event.get("delta") or "")
        return []

    def _item_done(self, event: dict[str, Any]) -> list[StreamEvent]:
        item = event.get("item")
        if not isinstance(item, dict):
            return []
        self._output_items.append(item)
        if item.get("type") != "function_call":
            return []
        item_id = str(event.get("item_id") or item.get("id") or "")
        entry = self._function_calls.get(item_id) or {}
        return [
            ToolCallDone(
                call_id=str(entry.get("call_id") or item.get("call_id") or ""),
                name=str(entry.get("name") or item.get("name") or ""),
                arguments=str(item.get("arguments") or entry.get("arguments") or ""),
                index=int(entry.get("index") or event.get("output_index") or 0),
            )
        ]

    def _terminal(self, event: dict[str, Any]) -> list[StreamEvent]:
        raw_response = event.get("response")
        response: dict[str, Any] = raw_response if isinstance(raw_response, dict) else {}
        self._response_id = str(response.get("id") or self._response_id)
        usage = response.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        output = response.get("output")
        if isinstance(output, list):
            self._output_items = [item for item in output if isinstance(item, dict)]
        self._completed = True
        return [
            Completed(
                response_id=self._response_id,
                output_items=self.output_items,
                usage=dict(self._usage),
            )
        ]

    def _failed(self, event: dict[str, Any]) -> list[StreamEvent]:
        raw_response = event.get("response")
        response: dict[str, Any] = raw_response if isinstance(raw_response, dict) else {}
        raw_error = response.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        self._completed = True
        return [
            Failed(
                code=(str(error.get("code")) or None),
                message=str(error.get("message") or "response.failed"),
            )
        ]

    def _bare_error(self, event: dict[str, Any]) -> list[StreamEvent]:
        self._completed = True
        return [
            Failed(
                code=(str(event.get("code")) or None), message=str(event.get("message") or "error")
            )
        ]
