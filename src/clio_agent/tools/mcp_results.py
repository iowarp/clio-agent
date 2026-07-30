"""Safe public projections of MCP tool-call results.

FastMCP returns two useful views of a tool result: a model-facing ``data``
object and the protocol's raw public content.  It may also carry private
``_meta`` capability data.  This module centralizes the boundary used by
durable telemetry so public JSON is retained without admitting private
metadata.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clio_agent.errors import MCP_RESULT_DOWNGRADED_TO_COMPLETE
from clio_agent.tools.mcp_runtime import wire_value

_MISSING = object()


@dataclass(frozen=True)
class MCPResultClassification:
    """Typed completion classification for one MCP tool-call result."""

    result_type: str
    degrade_reason: str | None = None
    explicitly_carried: bool = False
    original_result_type: str | None = None


def _explicit_result_type_classification(value: Any) -> MCPResultClassification:
    """Classify one explicitly carried resultType for the tasks-off client path."""

    result_type = str(value)
    if result_type == "complete":
        return MCPResultClassification(result_type=result_type, explicitly_carried=True)
    return MCPResultClassification(
        result_type="complete",
        degrade_reason=MCP_RESULT_DOWNGRADED_TO_COMPLETE,
        explicitly_carried=True,
        original_result_type=result_type,
    )


def classify_call_tool_result(result: Any) -> MCPResultClassification:
    """Classify an MCP result under the absent-means-complete protocol rule.

    Both supported protocol eras define an absent ``resultType`` as ordinary
    completeness. FastMCP's client-side ``CallToolResult`` dataclass carries no
    result-type field at all, while Pydantic protocol models can expose a
    defaulted field that is absent from ``model_fields_set``. Neither case is a
    downgrade. This tasks-off client records a downgrade only when the server
    explicitly carries a result type other than ``complete``.
    """

    if isinstance(result, Mapping):
        if "resultType" in result:
            return _explicit_result_type_classification(result["resultType"])
        if "result_type" in result:
            return _explicit_result_type_classification(result["result_type"])
        return MCPResultClassification(result_type="complete")

    fields_set = getattr(result, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(result, "__pydantic_fields_set__", None)
    if fields_set is not None:
        if "result_type" not in fields_set and "resultType" not in fields_set:
            return MCPResultClassification(result_type="complete")

    result_type = getattr(result, "result_type", _MISSING)
    if result_type is _MISSING:
        result_type = getattr(result, "resultType", _MISSING)
    if result_type is _MISSING:
        return MCPResultClassification(result_type="complete")
    return _explicit_result_type_classification(result_type)


def _public_content(content: Any) -> list[Any]:
    """Serialize MCP content blocks without their optional private metadata."""

    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return []
    public: list[Any] = []
    for block in content:
        wire = wire_value(block, mode="mcp_results", exclude_none=True)
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

    classification = classify_call_tool_result(result)
    observer: dict[str, Any] = {"content": _public_content(content)}
    if structured is not _MISSING and structured is not None:
        observer["structuredContent"] = wire_value(
            structured,
            mode="mcp_results",
            exclude_none=False,
        )
    if is_error is True:
        observer["isError"] = True
    if classification.explicitly_carried:
        observer["resultType"] = classification.result_type
    if classification.degrade_reason is not None:
        observer["degrade"] = {
            "reason": classification.degrade_reason,
            "resultType": classification.result_type,
            "originalResultType": classification.original_result_type,
        }
    return observer


__all__ = [
    "MCP_RESULT_DOWNGRADED_TO_COMPLETE",
    "MCPResultClassification",
    "call_tool_result_to_observer",
    "classify_call_tool_result",
]
