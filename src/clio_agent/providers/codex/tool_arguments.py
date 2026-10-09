"""Preserve complete Responses tool arguments when argument deltas are omitted."""

from __future__ import annotations

import json
from typing import Any

from dspy.lm15 import ServerError


class ToolArgumentCompletion:
    """Append only the missing suffix of a provider's completed function call.

    Some Codex replies, especially parallel calls, send ``output_item.added``
    followed directly by ``output_item.done``. lm15's delta parser does not read
    the latter's arguments. Keep streamed prefixes intact and reconcile the
    authoritative completed item before the response is materialized.
    """

    def __init__(self) -> None:
        self._arguments: dict[int, str] = {}
        self._identities: dict[int, tuple[str, str]] = {}

    def consume(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Track streamed arguments and return missing wire events before completion."""
        kind = payload.get("type")
        index = int(payload.get("output_index") or 0)
        item = payload.get("item") or {}
        if kind == "response.output_item.added" and item.get("type") == "function_call":
            self._identities[index] = self._identity(item)
            self._arguments[index] = str(item.get("arguments") or "")
        elif kind == "response.function_call_arguments.delta":
            self._arguments[index] = self._arguments.get(index, "") + str(
                payload.get("delta") or ""
            )
        elif kind == "response.output_item.done" and item.get("type") == "function_call":
            return self._complete(index, item)
        elif kind == "response.completed":
            events: list[dict[str, Any]] = []
            for output_index, output_item in enumerate(
                (payload.get("response") or {}).get("output") or []
            ):
                if isinstance(output_item, dict) and output_item.get("type") == "function_call":
                    events.extend(self._complete(output_index, output_item))
            return events
        return []

    def streamed_chars(self, index: int) -> int:
        """Return the argument length already forwarded for this output item."""
        return len(self._arguments.get(index, ""))

    @staticmethod
    def _identity(item: dict[str, Any]) -> tuple[str, str]:
        return str(item.get("call_id") or item.get("id") or ""), str(item.get("name") or "")

    def _complete(self, index: int, item: dict[str, Any]) -> list[dict[str, Any]]:
        identity = self._identity(item)
        final = str(item.get("arguments") or "")
        prefix = self._arguments.get(index, "")
        if index in self._identities and self._identities[index] != identity:
            raise ServerError("Codex changed a completed tool call's identity")
        try:
            arguments = json.loads(final)
        except json.JSONDecodeError as exc:
            raise ServerError("Codex returned invalid completed tool arguments") from exc
        if not isinstance(arguments, dict) or not final.startswith(prefix):
            raise ServerError("Codex returned inconsistent completed tool arguments")
        events: list[dict[str, Any]] = []
        if index not in self._identities:
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**item, "arguments": ""},
                }
            )
        if len(final) > len(prefix):
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": index,
                    "call_id": identity[0],
                    "name": identity[1],
                    "delta": final[len(prefix) :],
                }
            )
        self._identities[index] = identity
        self._arguments[index] = final
        return events
