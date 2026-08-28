"""Small value parsers shared by MCP declaration loading."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def optional_int(
    entry: Mapping[str, Any],
    key: str,
    errors: list[str],
    *,
    minimum: int | None = None,
    unit: str = "",
) -> int | None:
    """Parse one optional integer and append a declaration error on failure."""

    value = entry.get(key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"'{key}' must be an integer{unit}")
        return None
    if minimum is not None and parsed < minimum:
        errors.append(f"'{key}' must be zero or greater")
        return None
    return parsed
