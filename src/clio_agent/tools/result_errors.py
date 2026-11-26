"""A tool call's failure, read from its transport's own signal and nothing else.

A call failed when its transport says so: an MCP result's ``isError``, a native
tool's raise, or a native tool's own :class:`ToolFailure`. The words and keys of
a result never decide it (owner ruling, 2026-11): an ``error`` key, a
``status: failed``, an ``ok: false`` or a leading ``error:`` is the tool's report,
which the model reads like any other result, not a verdict clio guesses from it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

_TEXT_LIMIT = 500


class ToolFailure(dict[str, Any]):
    """A native tool's declaration that it could not do what was asked.

    The payload is exactly what the model reads, as it reads any result: returning it,
    rather than raising, keeps the tool's detail and next step in front of the model
    (``gact/a2ui_producer/_refusal.py``). The type is the verdict: the built-in MCP
    boundary carries it as ``isError`` and every reader of a call's outcome sees it as
    failed. A tool that answered with an empty or negative finding (no hits, no data
    for a date) returns a plain result instead.
    """


class FailedObservation(str):
    """The model-facing text of a call whose transport reported it failed (``isError``)."""


def declared_failure(value: Any) -> bool:
    """Whether ``value`` is a call's own failure: a :class:`ToolFailure` or failed text."""

    return isinstance(value, (ToolFailure, FailedObservation))


def observed_value(value: Any) -> Any:
    """``value`` as it is recorded: a :class:`FailedObservation` becomes plain text.

    The verdict is read (:func:`declared_failure`) before recording; encoders such as
    msgspec refuse a ``str`` subclass, so nothing past the call keeps the marker type.
    """

    return str(value) if isinstance(value, FailedObservation) else value


def structured_tool_result_error(result: Any) -> str | None:
    """The failure text of a call whose transport says it failed, else ``None``.

    Reads only the transport's signal: a :class:`ToolFailure` or
    :class:`FailedObservation`, or an MCP result's ``isError`` (a live result's
    attribute or a recorded result's key). The text is the result's own.
    """

    if isinstance(result, ToolFailure):
        return json.dumps(result, default=str, ensure_ascii=False)[:_TEXT_LIMIT]
    if isinstance(result, FailedObservation):
        return str(result).strip()[:_TEXT_LIMIT] or "tool_result_is_error"
    result_map = result if isinstance(result, Mapping) else {}
    explicitly_failed = getattr(
        result,
        "is_error",
        getattr(result, "isError", result_map.get("isError", result_map.get("is_error"))),
    )
    if explicitly_failed is not True:
        return None
    # A failed result's own text (e.g. a ToolError's code and message) beats a bare flag.
    return _first_text(result) or "tool_result_is_error"


def _first_text(result: Any, limit: int = _TEXT_LIMIT) -> str:
    """The first non-empty text block of a result, decoded from a JSON envelope when it is one."""
    result_map = result if isinstance(result, Mapping) else {}
    blocks = getattr(result, "content", None)
    if blocks is None:
        blocks = result_map.get("content")
    if isinstance(blocks, (str, bytes, bytearray)) or not isinstance(blocks, Sequence):
        return ""
    for block in blocks:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, Mapping):
            text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        stripped = text.strip()
        with suppress(json.JSONDecodeError, TypeError, ValueError):
            payload = json.loads(stripped)
            if isinstance(payload, Mapping) and payload.get("code") and payload.get("message"):
                return f"{payload['code']}: {payload['message']}"[:limit]
        return stripped[:limit]
    return ""


__all__ = [
    "FailedObservation",
    "ToolFailure",
    "declared_failure",
    "observed_value",
    "structured_tool_result_error",
]
