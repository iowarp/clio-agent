"""Stateful projection of correlated tool progress into transcript payloads."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import Any


def _terminal_progress_chunk(message: object) -> tuple[str, str] | None:
    """Decode CLIO's typed terminal-chunk progress envelope, if present."""

    if not isinstance(message, str) or not message:
        return None
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping) or payload.get("type") != "clio.terminal.chunk":
        return None
    stream = str(payload.get("stream") or "stdout")
    text = payload.get("text")
    if stream not in {"stdout", "stderr"} or not isinstance(text, str):
        return None
    return stream, text


class ToolProgressRegistry:
    """Correlate live deltas and retain complete output for durable presentation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._output_by_call: dict[str, str] = {}

    def started(self, call_id: str, session_id: str) -> dict[str, str]:
        """Register a call and return its thread-crossing correlation handle."""

        with self._lock:
            self._output_by_call[call_id] = ""
        return {"call_id": call_id, "session_id": session_id}

    def completed(self, call_id: str) -> str:
        """Release retained progress state for a terminal call."""

        with self._lock:
            return self._output_by_call.pop(call_id, "")

    def project(self, tool_name: str, result: object) -> tuple[str, dict[str, Any], object] | None:
        """Project one progress notification, or return no row if uncorrelated."""

        progress = result if isinstance(result, Mapping) else {}
        handle = progress.get("observer_handle")
        handle_row = handle if isinstance(handle, Mapping) else {}
        call_id = str(handle_row.get("call_id") or "")
        session_id = str(handle_row.get("session_id") or "")
        if not call_id or not session_id:
            return None
        payload: dict[str, Any] = {
            "call_id": call_id,
            "tool": tool_name,
            "progress": progress.get("progress"),
            "total": progress.get("total"),
        }
        message = progress.get("message")
        terminal_chunk = _terminal_progress_chunk(message)
        if terminal_chunk is not None:
            stream, chunk = terminal_chunk
            rendered = chunk
            with self._lock:
                if call_id not in self._output_by_call:
                    return None
                previous = self._output_by_call[call_id]
                retained = previous + rendered
                self._output_by_call[call_id] = retained
            payload["output_stream"] = retained
            payload["presentation_delta"] = {
                "call_id": call_id,
                "block_id": "terminal",
                "offset": len(previous),
                "sequence": len(previous),
                "channel": stream,
                "text": rendered,
            }
        elif isinstance(message, str) and message:
            payload["progress_message"] = message
        return session_id, payload, handle


__all__ = ["ToolProgressRegistry"]
