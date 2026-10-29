"""Select JSON result fields without changing their immutable source or read authority."""

from __future__ import annotations

import json
import re
from typing import Any


def validate_selection(select: list[str] | None) -> list[str] | None:
    """Validate a bounded list of RFC 6901 JSON Pointers, preserving selection order."""
    if select is None:
        return None
    if (
        not isinstance(select, list)
        or not 1 <= len(select) <= 32
        or any(
            not isinstance(pointer, str)
            or len(pointer) > 1024
            or (pointer != "" and not pointer.startswith("/"))
            or re.search(r"~(?![01])", pointer)
            for pointer in select
        )
        or len(set(select)) != len(select)
    ):
        raise ValueError("select must contain 1–32 distinct valid JSON Pointers")
    return select


def select_fields(value: Any, select: list[str] | None) -> Any:
    """Return pointer-keyed selected values; a missing path is an explicit read error."""
    if select is None:
        return value
    result: dict[str, Any] = {}
    for pointer in select:
        current = value
        for raw in pointer.split("/")[1:] if pointer else ():
            key = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                # Bound before conversion: huge numeric tokens cannot name a list member.
                if len(key) > len(str(len(current))) or int(key) >= len(current):
                    raise ValueError(f"Selected result path is unavailable: {pointer}")
                current = current[int(key)]
            else:
                raise ValueError(f"Selected result path is unavailable: {pointer}")
        result[pointer] = current
    return result


def result_keys(value: dict[str, Any]) -> dict[str, Any]:
    """Expose a small key inventory without repeating arbitrarily large result keys."""
    keys: list[str] = []
    size = 0
    for key in value:
        size += len(json.dumps(key, ensure_ascii=False)) + 2
        if len(keys) == 64 or size > 1024:
            break
        keys.append(key)
    return {"result_keys": keys, "result_keys_truncated": len(keys) != len(value)}
