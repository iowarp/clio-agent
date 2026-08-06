"""Safe public projections of MCP tool-call results.

FastMCP returns two useful views of a tool result: a model-facing ``data``
object and the protocol's raw public content.  It may also carry private
``_meta`` capability data.  This module centralizes the boundary used by
durable telemetry so public JSON is retained without admitting private
metadata.

Content-block bounding (P5 wire semantics, #1188 MCP content-block half): a
kit tool's typed content blocks (``ImageContent``, ``AudioContent``, an
``EmbeddedResource`` blob) can carry an arbitrarily large base64 payload —
:func:`_public_content` is the ONE seam every downstream consumer of a tool
result inherits from (the live wire bus event, the tool-call ledger, the
durable semantic-event trace, and the ``tool_result`` Part's
``content_blocks`` field all read the same projected dict), so eliding an
oversized binary field HERE, once, bounds all of them. The elision marker
mirrors the artifact-content idiom
(``gact/agents/tool_instrumentation._elided_artifact_content``): declare the
degradation with a typed reason + byte count, never silently truncate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clio_agent import conf
from clio_agent.errors import MCP_RESULT_DOWNGRADED_TO_COMPLETE
from clio_agent.tools.mcp_runtime import wire_value

_MISSING = object()

#: Typed reason recorded on an elided content block (never a silent drop).
CONTENT_BLOCK_ELISION_REASON = "content_block_oversize"

#: Live-wire cap for one MCP content block's decoded binary payload (an
#: ImageContent/AudioContent ``data`` field, or an EmbeddedResource's
#: ``blob``). A block over this size is replaced by a typed elision marker
#: (``{"elided": ..., "bytes": N}``, payload field dropped) before it ever
#: leaves this module. 512 KiB comfortably covers a typical plot/chart PNG
#: while keeping one tool result from ballooning a live SSE frame or the
#: durable trace.
MAX_CONTENT_BLOCK_BYTES: int = conf.resolve(
    "limits.mcp_content_block_max_bytes",
    env="CLIO_MCP_CONTENT_BLOCK_MAX_BYTES",
    default=512 * 1024,
    cast=conf.as_int,
)

#: Result types this client HANDLES, so carrying one is not a degrade.
#:
#: ``complete`` is the protocol's ordinary completeness. ``task`` joined it in
#: #1115: CLIO now declares the SEP-2663 tasks extension on every execution-path
#: client (:mod:`clio_agent.tools.mcp_tasks`), so a ``CreateTaskResult`` is a shape
#: the client drives to the real result — recording it as a downgrade-to-complete
#: would report a working path as a degradation. A result type outside this set is
#: still one the client cannot act on, and still degrades.
HANDLED_RESULT_TYPES: frozenset[str] = frozenset({"complete", "task"})


@dataclass(frozen=True)
class MCPResultClassification:
    """Typed completion classification for one MCP tool-call result."""

    result_type: str
    degrade_reason: str | None = None
    explicitly_carried: bool = False
    original_result_type: str | None = None


def _explicit_result_type_classification(value: Any) -> MCPResultClassification:
    """Classify one explicitly carried resultType against the handled set."""

    result_type = str(value)
    if result_type in HANDLED_RESULT_TYPES:
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
    downgrade. A downgrade is recorded only when the server explicitly carries a
    result type outside :data:`HANDLED_RESULT_TYPES` — which, since #1115, includes
    the tasks extension's ``task``.
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


def _base64_decoded_length(data: str) -> int:
    """Approximate decoded byte length of a base64 string without decoding it."""

    if not data:
        return 0
    padding = len(data) - len(data.rstrip("="))
    return max(0, (len(data) * 3) // 4 - padding)


def _elide_oversized_binary_field(block: dict[str, Any], field: str) -> dict[str, Any]:
    """Replace ``block[field]`` with a typed elision marker when it is oversized.

    The reason + byte count are declared IN THE BLOCK (never a silent drop) —
    every other field (``type``, ``mimeType``, ...) is kept so the caller still
    knows what kind of evidence was elided.
    """

    value = block.get(field)
    if not isinstance(value, str) or not value:
        return block
    size = _base64_decoded_length(value)
    if size <= MAX_CONTENT_BLOCK_BYTES:
        return block
    elided = {key: item for key, item in block.items() if key != field}
    elided["elided"] = CONTENT_BLOCK_ELISION_REASON
    elided["bytes"] = size
    return elided


def _elide_oversized_content_block(block: Any) -> Any:
    """Bound one wire-projected content block's binary payload, if any.

    ``image``/``audio`` blocks carry the payload directly on ``data``; a
    ``resource`` block (``EmbeddedResource``) nests a blob resource under
    ``resource.blob``. Every other block shape (``text``, ``resource_link``)
    passes through unchanged — they carry no unbounded binary field.
    """

    if not isinstance(block, Mapping):
        return block
    block = dict(block)
    block_type = block.get("type")
    if block_type in ("image", "audio"):
        return _elide_oversized_binary_field(block, "data")
    if block_type == "resource" and isinstance(block.get("resource"), Mapping):
        resource = _elide_oversized_binary_field(dict(block["resource"]), "blob")
        block["resource"] = resource
    return block


def _public_content(content: Any) -> list[Any]:
    """Serialize MCP content blocks without their optional private metadata.

    Every block is also passed through :func:`_elide_oversized_content_block`
    so an oversized image/audio/resource payload never reaches a downstream
    consumer as raw base64 (see module docstring: this is the ONE seam every
    consumer of a tool result inherits from).
    """

    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return []
    public: list[Any] = []
    for block in content:
        wire = wire_value(block, mode="mcp_results", exclude_none=True)
        if isinstance(wire, Mapping):
            wire = dict(wire)
            wire.pop("_meta", None)
            wire.pop("meta", None)
            wire = _elide_oversized_content_block(wire)
        public.append(wire)
    return public


def content_blocks_for_wire(observer_result: Any) -> list[dict[str, Any]] | None:
    """Return one observer projection's content blocks for a wire ``Part``.

    ``observer_result`` is the dict :func:`call_tool_result_to_observer`
    produced (already bounded/elided). A native tool's plain return is never
    MCP-shaped, and an MCP result with an EMPTY ``content`` list both yield
    ``None`` -- never an invented empty list on the wire (mirrors
    ``gact/parts.py``'s ``Part.content_blocks`` field, which this feeds).
    """

    content = observer_result.get("content") if isinstance(observer_result, Mapping) else None
    return list(content) if isinstance(content, list) and content else None


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
    "CONTENT_BLOCK_ELISION_REASON",
    "HANDLED_RESULT_TYPES",
    "MAX_CONTENT_BLOCK_BYTES",
    "MCP_RESULT_DOWNGRADED_TO_COMPLETE",
    "MCPResultClassification",
    "call_tool_result_to_observer",
    "classify_call_tool_result",
    "content_blocks_for_wire",
]
