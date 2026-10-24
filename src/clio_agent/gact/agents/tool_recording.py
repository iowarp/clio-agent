"""Tool wrappers that preserve typed tool-call evidence."""

from __future__ import annotations

import inspect
import time
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import skill_runtime as _skill_runtime
from clio_agent.tools.result_errors import structured_tool_result_error

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef


def recorded_load_skill_tool(agent_def: AgentDef, skill_rt: Any) -> Any:
    """Build a causally recorded ``load_skill`` tool."""
    return recording_tool(_skill_runtime.build_load_skill_tool(agent_def, skill_rt))


def recorded_spawn_skill_task_tool(agent_def: AgentDef, skill_rt: Any) -> Any:
    """Build the recorded launcher for a skill-seeded child turn."""
    return recording_tool(_skill_runtime.build_spawn_skill_task_tool(agent_def, skill_rt))


def recording_tool(tool: Any) -> Any:
    """Wrap a DSPy tool so agent ReAct predictions retain tool evidence."""
    from clio_agent.gact.agents.tool_instrumentation import rebuilt_tool  # noqa: PLC0415
    from clio_agent.gact.app import _bounded_tool_call_result  # noqa: PLC0415

    name = str(getattr(tool, "name", "") or "").strip()
    desc = str(getattr(tool, "desc", "") or getattr(tool, "__doc__", "") or name)
    args = getattr(tool, "args", None) or {}

    def record_failure(
        rows: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
        started_at: float,
        exc: BaseException,
    ) -> None:
        if rows is not None:
            rows.append(
                {
                    "name": name,
                    "args": dict(kwargs),
                    "ok": False,
                    "duration_ms": (time.perf_counter() - started_at) * 1000,
                    "error": str(exc),
                    "telemetry_source": "agent_react_tool_wrapper",
                }
            )

    def record_result(
        rows: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
        started_at: float,
        result: Any,
    ) -> None:
        tool_error = structured_tool_result_error(result)
        row_result = _bounded_tool_call_result(result)
        if rows is not None:
            row = {
                "name": name,
                "args": dict(kwargs),
                "ok": tool_error is None,
                "duration_ms": (time.perf_counter() - started_at) * 1000,
                "result": row_result,
                "telemetry_source": "agent_react_tool_wrapper",
            }
            if tool_error is not None:
                row["error"] = tool_error
            rows.append(row)

    if inspect.iscoroutinefunction(getattr(tool, "func", None)):

        async def async_call_tool(**kwargs: Any) -> Any:
            started_at = time.perf_counter()
            rows = _ctx.active_agent_tool_rows()
            try:
                result = await tool.acall(**kwargs)
            except BaseException as exc:  # noqa: BLE001 - record cancellation, then propagate
                record_failure(rows, kwargs, started_at, exc)
                raise
            record_result(rows, kwargs, started_at, result)
            return result

        async_call_tool.__name__ = name
        async_call_tool.__doc__ = desc
        return rebuilt_tool(tool, async_call_tool, name=name, desc=desc, args=args)

    def call_tool(**kwargs: Any) -> Any:
        started_at = time.perf_counter()
        rows = _ctx.active_agent_tool_rows()
        try:
            result = tool(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            record_failure(rows, kwargs, started_at, exc)
            raise
        record_result(rows, kwargs, started_at, result)
        return result

    call_tool.__name__ = name
    call_tool.__doc__ = desc
    return rebuilt_tool(tool, call_tool, name=name, desc=desc, args=args)
