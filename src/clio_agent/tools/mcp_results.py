"""Safe public projections of MCP tool-call results.

FastMCP returns two useful views of a tool result: a model-facing ``data``
object and the protocol's raw public content.  It may also carry private
``_meta`` capability data.  This module centralizes the boundary used by
durable telemetry so public JSON is retained without admitting private
metadata.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_MISSING = object()


def _model_dump(value: Any, *, exclude_none: bool) -> Any:
    """Return a JSON-mode Pydantic dump while supporting lightweight fakes."""

    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        return _MISSING
    attempts: tuple[dict[str, Any], ...] = (
        {"mode": "json", "by_alias": True, "exclude_none": exclude_none},
        {"by_alias": True, "exclude_none": exclude_none},
        {},
    )
    for kwargs in attempts:
        try:
            return dump(**kwargs)
        except TypeError:
            continue
    return _MISSING


def _wire_value(value: Any, *, exclude_none: bool) -> Any:
    """Recursively convert SDK and Pydantic values to JSON-compatible data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _wire_value(item, exclude_none=exclude_none)
            for key, item in value.items()
            if not (exclude_none and item is None)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_wire_value(item, exclude_none=exclude_none) for item in value]
    dumped = _model_dump(value, exclude_none=exclude_none)
    if dumped is not _MISSING:
        return _wire_value(dumped, exclude_none=exclude_none)
    return str(value)


def _public_content(content: Any) -> list[Any]:
    """Serialize MCP content blocks without their optional private metadata."""

    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return []
    public: list[Any] = []
    for block in content:
        wire = _wire_value(block, exclude_none=True)
        if isinstance(wire, Mapping):
            wire = dict(wire)
            wire.pop("_meta", None)
            wire.pop("meta", None)
        public.append(wire)
    return public


def call_tool_result_to_observer(result: Any) -> dict[str, Any]:
    """Return exact public MCP result data safe for durable tool telemetry.

    ``structured_content`` is authoritative when the FastMCP result exposes it.
    Some bridge and output-schema paths instead expose only a validated Pydantic
    object through ``data``.  In that case the JSON-mode, alias-preserving dump
    is recorded as ``structuredContent``.  Result-level and content-block
    ``_meta`` values are intentionally excluded.
    """

    result_map = result if isinstance(result, Mapping) else {}
    content = getattr(result, "content", result_map.get("content", [])) or []

    structured = getattr(result, "structured_content", _MISSING)
    if structured is _MISSING:
        structured = getattr(result, "structuredContent", _MISSING)
    if structured is _MISSING:
        structured = result_map.get(
            "structuredContent",
            result_map.get("structured_content", _MISSING),
        )
    if structured is _MISSING or structured is None:
        data = getattr(result, "data", result_map.get("data", _MISSING))
        if data is not _MISSING and data is not None:
            structured = data

    is_error = getattr(result, "is_error", _MISSING)
    if is_error is _MISSING:
        is_error = getattr(result, "isError", _MISSING)
    if is_error is _MISSING:
        is_error = result_map.get("isError", result_map.get("is_error", False))

    observer: dict[str, Any] = {"content": _public_content(content)}
    if structured is not _MISSING and structured is not None:
        observer["structuredContent"] = _wire_value(structured, exclude_none=False)
    if is_error is True:
        observer["isError"] = True
    return observer


__all__ = ["call_tool_result_to_observer"]
