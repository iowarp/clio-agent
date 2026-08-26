"""Structured tool-result failure extraction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import suppress
from typing import Any


def structured_tool_result_error(result: Any) -> str | None:
    """Return an error string when a tool returns a structured error payload."""

    decoded = result
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{") and '"error"' in stripped:
            with suppress(json.JSONDecodeError, TypeError):
                decoded = json.loads(stripped)
    if isinstance(decoded, Mapping):
        error = decoded.get("error")
        if error:
            if isinstance(error, Mapping):
                code = str(error.get("code") or error.get("type") or "tool_error")
                message = str(error.get("message") or "").strip()
                return f"{code}: {message}" if message else code
            return str(error)
        status = str(decoded.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            message = str(decoded.get("message") or decoded.get("detail") or "").strip()
            return f"status={status}: {message}" if message else f"status={status}"
        if decoded.get("ok") is False:
            message = str(decoded.get("message") or decoded.get("detail") or "").strip()
            return f"ok=false: {message}" if message else "ok=false"
    elif isinstance(decoded, str) and decoded.strip().casefold().startswith("error:"):
        return decoded.strip()

    result_map = result if isinstance(result, Mapping) else {}
    candidates = (
        getattr(result, "structured_content", None),
        getattr(result, "structuredContent", None),
        result_map.get("structuredContent"),
        result_map.get("structured_content"),
        getattr(result, "data", None),
        result_map.get("data"),
    )
    for candidate in candidates:
        if candidate is not None and candidate is not result:
            if nested_error := structured_tool_result_error(candidate):
                return nested_error
    explicitly_failed = getattr(
        result,
        "is_error",
        getattr(result, "isError", result_map.get("isError", result_map.get("is_error"))),
    )
    return "tool_result_is_error" if explicitly_failed is True else None


__all__ = ["structured_tool_result_error"]
