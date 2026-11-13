"""Bound a tool result's structured content on its way to the wire and the trace.

A server may return a large structured payload that only a program should read: the
``hdf5-ptc`` server returns a whole array as ``structuredContent`` when a ``run_code``
script asks for one. The model never sees it (:func:`clio_agent.tools.mcp_result_text.
result_to_text` gives the model the text blocks), and the calling script reads the
full value from the result sink before the observer runs. What remains are the copies
the observer writes: the live ``tool_result`` part (SSE and the stored transcript) and
the trace event. Those copies are bounded here, with a typed reason, so one scripted
read of a 16 MB array does not land in the transcript and the event log.

Config: ``limits.tool_structured_bytes`` / ``CLIO_TOOL_STRUCTURED_BYTES`` (default
262144 bytes of compact JSON).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: Typed reason recorded when a structured payload is replaced by its envelope.
STRUCTURED_CONTENT_OVER_WIRE_CAP_REASON = "structured_content_over_wire_cap"
#: The key a bounded envelope carries, so a reader can tell it from a tool's own payload.
BOUNDED_MARKER = "clio_structured_content_bounded"


def tool_structured_bytes() -> int:
    """The byte bound on one result's structured content in the transcript and trace."""

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "limits.tool_structured_bytes",
        env="CLIO_TOOL_STRUCTURED_BYTES",
        default=262_144,
        cast=conf.as_int,
    )


def _size(value: Any) -> int:
    """Compact JSON size of ``value`` in bytes (``repr`` for what JSON cannot encode)."""

    try:
        return len(json.dumps(value, separators=(",", ":"), default=repr).encode("utf-8"))
    except (TypeError, ValueError):
        return len(repr(value).encode("utf-8"))


def _envelope(value: Any, size: int, limit: int) -> dict[str, Any]:
    """The stand-in for an over-cap payload: what it was, never its contents."""

    keys = sorted(str(key) for key in value)[:20] if isinstance(value, Mapping) else []
    envelope: dict[str, Any] = {
        BOUNDED_MARKER: True,
        "reason": STRUCTURED_CONTENT_OVER_WIRE_CAP_REASON,
        "original_bytes": size,
        "limit_bytes": limit,
    }
    if keys:
        envelope["keys"] = keys
    if isinstance(value, Mapping):
        for name in ("shape", "dtype", "path", "file"):
            if isinstance(value.get(name), (str, int, float, list)):
                envelope[name] = value[name]
    return envelope


def bounded_structured_content(value: Any, *, limit: int | None = None) -> Any:
    """``value`` when it fits the wire bound, else a typed envelope describing it."""

    if value is None:
        return None
    if isinstance(value, Mapping) and value.get(BOUNDED_MARKER):
        return value
    limit = tool_structured_bytes() if limit is None else limit
    size = _size(value)
    if size <= limit:
        return value
    logger.info(
        "structured content bounded reason=%s original_bytes=%d limit_bytes=%d",
        STRUCTURED_CONTENT_OVER_WIRE_CAP_REASON,
        size,
        limit,
    )
    return _envelope(value, size, limit)


def wire_structured(declared: Any, result: Any) -> tuple[Any, Any]:
    """The observer's structured copy and result, both bounded for the wire and trace.

    A native tool's declared payload wins; else an MCP result's ``structuredContent``.
    """

    if declared is None and isinstance(result, Mapping):
        declared = result.get("structuredContent")
    return bounded_structured_content(declared), bounded_result_structured(result)


def bounded_result_structured(result: Any, *, limit: int | None = None) -> Any:
    """``result`` with its ``structuredContent`` bounded (a shallow copy when it changes)."""

    if not isinstance(result, Mapping) or "structuredContent" not in result:
        return result
    bounded = bounded_structured_content(result["structuredContent"], limit=limit)
    if bounded is result["structuredContent"]:
        return result
    return {**result, "structuredContent": bounded}
