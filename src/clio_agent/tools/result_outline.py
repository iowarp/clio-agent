"""The shape of a JSON tool result, so an agent can query a saved result without reading it.

A spilled result (:mod:`clio_agent.tools.mcp_result_projection`) is saved to a file. To
explore it with a script or a search, the agent needs its structure: which keys, how
long each list is, and which fields its items have. This is a structural description
only: no value is summarized, chosen or interpreted.

    {
      datasets: list of 100 x object {id, title, resources: list of 0-1000 x object {...}}
      total: int
    }
"""

from __future__ import annotations

from typing import Any

#: Keys listed per object before the rest are counted.
MAX_KEYS = 24
#: Nesting described before an object or list is shown as ``{...}`` / ``[...]``.
MAX_DEPTH = 6
#: Characters of the whole outline; past it, the outline says it was cut.
MAX_CHARS = 1_500


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return type(value).__name__


def _merged_keys(items: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Every key the objects use, in first-seen order, with all their values."""
    keys: dict[str, list[Any]] = {}
    for item in items:
        for key, value in item.items():
            keys.setdefault(key, []).append(value)
    return keys


def _describe_values(values: list[Any], depth: int) -> str:
    """One description for the values one key (or one list) holds."""
    present = [v for v in values if v is not None]
    nullable = " | null" if len(present) < len(values) else ""
    objects = [v for v in present if isinstance(v, dict)]
    lists = [v for v in present if isinstance(v, list)]
    if objects and len(objects) == len(present):
        return _describe_object(objects, depth) + nullable
    if lists and len(lists) == len(present):
        return _describe_list(lists, depth) + nullable
    kinds = sorted({_kind(v) for v in values})
    return " | ".join(kinds)


def _describe_object(objects: list[dict[str, Any]], depth: int) -> str:
    if depth >= MAX_DEPTH:
        return "object {...}"
    keys = _merged_keys(objects)
    parts = []
    for key in list(keys)[:MAX_KEYS]:
        values = keys[key]
        nested = any(isinstance(v, (dict, list)) for v in values)
        parts.append(f"{key}: {_describe_values(values, depth + 1)}" if nested else key)
    if len(keys) > MAX_KEYS:
        parts.append(f"... {len(keys) - MAX_KEYS} more keys")
    return "object {" + ", ".join(parts) + "}"


def _describe_list(lists: list[list[Any]], depth: int) -> str:
    lengths = [len(v) for v in lists]
    low, high = min(lengths), max(lengths)
    size = str(low) if low == high else f"{low}-{high}"
    items = [item for value in lists for item in value]
    if not items:
        return f"list of {size}"
    if depth >= MAX_DEPTH:
        return f"list of {size} x [...]"
    return f"list of {size} x {_describe_values(items, depth + 1)}"


def json_outline(value: Any) -> str:
    """The structure of a parsed JSON value, one top-level key a line."""
    if isinstance(value, dict):
        lines = ["{"]
        for key in list(value)[:MAX_KEYS]:
            lines.append(f"  {key}: {_describe_values([value[key]], 1)}")
        if len(value) > MAX_KEYS:
            lines.append(f"  ... {len(value) - MAX_KEYS} more keys")
        lines.append("}")
        text = "\n".join(lines)
    else:
        text = _describe_values([value], 0)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n  ... (outline cut)"
    return text
