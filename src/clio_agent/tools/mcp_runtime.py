"""Shared helpers for MCP runtime pathways.

Three historical wire contracts are preserved as explicit :func:`wire_value`
modes: ``mcp_results``, ``mcp_apps``, and ``gact_runtime``. Converging those
contracts is future wire-work, not part of this slice. The single MCP client
factory planned for #1106 will live here too.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

WireMode = Literal["mcp_results", "mcp_apps", "gact_runtime"]

_MISSING = object()
_VALID_MODES: frozenset[str] = frozenset(("mcp_results", "mcp_apps", "gact_runtime"))


def _dump_mcp_results_model(value: Any, *, exclude_none: bool) -> Any:
    """Return the historical MCP-results Pydantic projection when available."""

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


def wire_value(
    value: Any,
    *,
    mode: WireMode,
    exclude_none: bool = False,
) -> Any:
    """Convert an SDK or Pydantic value using an explicit historical contract.

    Args:
        value: Value to recursively convert to plain wire data.
        mode: Historical contract to preserve. ``mcp_results`` uses JSON-mode,
            alias-preserving Pydantic dumps; ``mcp_apps`` uses its Python-mode,
            model-None-excluding behavior; ``gact_runtime`` preserves the
            runtime trace's tuple and sorted-set handling.
        exclude_none: Mapping and Pydantic-field filter used only by the
            ``mcp_results`` contract.

    Returns:
        A recursively converted value matching the selected historical output.

    Raises:
        ValueError: If ``mode`` does not name a preserved wire contract.
    """

    if mode not in _VALID_MODES:
        raise ValueError(f"unknown MCP wire mode: {mode!r}")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if mode == "mcp_results":
        if isinstance(value, Mapping):
            return {
                str(key): wire_value(item, mode=mode, exclude_none=exclude_none)
                for key, item in value.items()
                if not (exclude_none and item is None)
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [wire_value(item, mode=mode, exclude_none=exclude_none) for item in value]
        dumped = _dump_mcp_results_model(value, exclude_none=exclude_none)
        if dumped is not _MISSING:
            return wire_value(dumped, mode=mode, exclude_none=exclude_none)
        return str(value)

    if mode == "mcp_apps":
        if isinstance(value, Mapping):
            return {str(key): wire_value(item, mode=mode) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [wire_value(item, mode=mode) for item in value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return wire_value(dump(by_alias=True, exclude_none=True), mode=mode)
        return str(value)

    if isinstance(value, Mapping):
        return {str(key): wire_value(item, mode=mode) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_value(item, mode=mode) for item in value]
    if isinstance(value, set):
        return sorted(wire_value(item, mode=mode) for item in value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return wire_value(model_dump(exclude_none=True), mode=mode)
        except TypeError:
            return wire_value(model_dump(), mode=mode)
    return str(value)


__all__ = ["WireMode", "wire_value"]
