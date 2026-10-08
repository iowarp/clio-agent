"""Keep completed Codex tool arguments when the wire coalesces fragment events."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Iterator
from typing import Any

from dspy.lm15 import StreamDeltaEvent, ToolCallDelta, TransportError


class ToolCallAssembler:
    """Reconcile argument fragments with the backend's authoritative completed item."""

    def __init__(self) -> None:
        self.arguments: dict[int, str] = {}
        self.items: dict[str, int] = {}
        self.headers: dict[int, tuple[str, str]] = {}

    def parse(self, wire: Any, request: Any, event: Any) -> list[Any]:
        """Preserve full final arguments without duplicating streamed fragments or calls."""
        payload = json.loads(event.data)
        item = payload.get("item")
        kind = str(payload.get("type") or event.event)
        index = int(payload.get("output_index", 0))
        if isinstance(item, dict) and item.get("type") == "function_call":
            identity = str(item.get("id") or "")
            if identity:
                self.items[identity] = index
            header = (str(item.get("call_id") or identity), str(item.get("name") or ""))
            previous = self.headers.get(index)
            if previous is not None and header != previous:
                raise TransportError("Codex completed tool identity disagrees with its stream")
            self.headers[index] = header
            if kind == "response.output_item.done":
                return self._complete(index, item.get("arguments"))
        if kind.startswith("response.function_call_arguments."):
            identity = str(payload.get("item_id") or "")
            if identity in self.items:
                index = self.items[identity]
                payload["output_index"] = index
                event = dataclasses.replace(event, data=json.dumps(payload))
            if kind.endswith(".done") and index in self.headers:
                return self._complete(index, payload.get("arguments"))
        return list(self.stream(wire.parse_stream_events(request, event)))

    def stream(self, events: Iterable[Any]) -> Iterator[Any]:
        """Apply the same completed-item contract to canonical HTTP/SSE events."""
        for part in events:
            delta = getattr(part, "delta", None)
            if isinstance(delta, ToolCallDelta):
                self.arguments[delta.part_index] = (
                    self.arguments.get(delta.part_index, "") + delta.input
                )
                if delta.id is not None and delta.name is not None:
                    self.headers[delta.part_index] = (delta.id, delta.name)
            if getattr(part, "type", "") == "end":
                data = getattr(part, "provider_data", None) or {}
                for index, item in enumerate(data.get("output") or []):
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        header = (
                            str(item.get("call_id") or item.get("id") or ""),
                            str(item.get("name") or ""),
                        )
                        if index in self.headers and self.headers[index] != header:
                            raise TransportError(
                                "Codex final tool identity disagrees with its stream"
                            )
                        self.headers[index] = header
                        yield from self._complete(index, item.get("arguments"))
            yield part

    def _complete(self, index: int, final: Any) -> list[Any]:
        if not isinstance(final, str):
            raise TransportError("Codex completed tool arguments are not a JSON string")
        try:
            value = json.loads(final)
        except json.JSONDecodeError as exc:
            raise TransportError("Codex completed tool arguments are invalid JSON") from exc
        if not isinstance(value, dict):
            raise TransportError("Codex completed tool arguments must be an object")
        current = self.arguments.get(index, "")
        if final.startswith(current):
            suffix = final[len(current) :]
        else:
            try:
                equivalent = json.loads(current) == value
            except json.JSONDecodeError:
                equivalent = False
            if not equivalent:
                raise TransportError(
                    "Codex completed tool arguments disagree with streamed fragments"
                )
            suffix = ""
        if not suffix:
            return []
        self.arguments[index] = final
        identity, name = self.headers[index]
        return [
            StreamDeltaEvent(
                delta=ToolCallDelta(part_index=index, id=identity, name=name, input=suffix)
            )
        ]
