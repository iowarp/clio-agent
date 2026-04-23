"""Native expert tool execution helpers."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from clio_agent.harness import (
    ToolObservation,
    normalize_tool_error,
    normalize_tool_result,
    tool_result_ok,
)
from clio_agent.tools.execution import ToolExecutor


class NativeToolRunner:
    """Run deterministic tools and retain CLIO provenance for the expert result."""

    def __init__(self, tool_executor: ToolExecutor) -> None:
        self._tool_executor = tool_executor
        self._tools: list[ToolObservation] = []

    @property
    def observations(self) -> tuple[ToolObservation, ...]:
        """Return recorded tool observations."""
        return tuple(self._tools)

    def call(self, name: str, params: Mapping[str, Any]) -> Any:
        """Call one tool through the explicit executor boundary."""
        start = time.time()
        try:
            raw_result = self._tool_executor.call_tool(name, dict(params))
            result = normalize_tool_result(self._decode_result(raw_result), tool=name)
        except Exception as exc:
            result = {"error": normalize_tool_error(exc, tool=name, code="tool_exception")}
        duration_ms = (time.time() - start) * 1000
        self._tools.append(
            ToolObservation(
                tool=name,
                params=dict(params),
                result=result,
                duration_ms=duration_ms,
                ok=tool_result_ok(result),
            )
        )
        return result

    def mark_validation_error(self, name: str, error: dict[str, Any]) -> None:
        """Replace a successful-looking observation with a contract failure."""
        for observation in reversed(self._tools):
            if observation.tool == name:
                observation.result = {"error": normalize_tool_error(error, tool=name)}
                observation.ok = False
                return

    @staticmethod
    def _decode_result(raw_result: Any) -> Any:
        if isinstance(raw_result, str):
            try:
                return json.loads(raw_result)
            except json.JSONDecodeError:
                return raw_result
        return raw_result
