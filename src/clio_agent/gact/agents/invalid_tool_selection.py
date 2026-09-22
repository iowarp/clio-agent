"""Typed evidence for a dynamic agent selecting an unavailable tool."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.globals import (
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _emit_semantic_event,
)

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef


def _invalid_tool_selection_from_exception(
    exc: BaseException,
    *,
    allowed_tools: Iterable[str],
) -> str:
    """Extract a rejected tool name from DSPy parser/validation errors."""

    allowed = {str(name).strip() for name in allowed_tools if str(name).strip()}
    message = str(exc)
    candidates: list[str] = []
    for pattern in (
        r"next_tool_name\s+with\s+value\s+[`'\"]?([^`'\"\s,\)]+)",
        r"tool_name\s+with\s+value\s+[`'\"]?([^`'\"\s,\)]+)",
        r"[`'\"]([^`'\"]+)[`'\"]\s+is\s+not\s+one\s+of\s+\(",
        r"invalid\s+tool\s+[`'\"]?([^`'\"\s,\)]+)",
    ):
        candidates.extend(match.group(1).strip() for match in re.finditer(pattern, message, re.I))
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:")
        if candidate and candidate not in allowed:
            return candidate
    return ""


def _emit_invalid_tool_selection_event(
    app: Any,
    sid: str,
    agent_def: "AgentDef",
    *,
    requested_tool: str,
    allowed_tools: Iterable[str],
    exc: BaseException,
) -> None:
    """Publish blocked invalid-tool selection evidence for live and durable traces."""

    allowed = sorted({str(name).strip() for name in allowed_tools if str(name).strip()})
    payload = {
        "agent_id": agent_def.id,
        "agent_title": agent_def.title,
        "requested_tool": requested_tool,
        "allowed_tools": allowed,
        "tool_executed": False,
        "recovery_status": "failed",
        "error_type": type(exc).__name__,
        "error_message": str(exc)[:1000],
        "error_full": str(exc),
    }
    summary = (
        f"Expert {agent_def.id!r} selected unavailable tool {requested_tool!r}; "
        "CLIO blocked execution."
    )
    if hasattr(getattr(app, "state", None), "bus"):
        app.state.bus.publish(
            Event(
                type="tool.selection.invalid",
                session_id=sid,
                payload={
                    **payload,
                    "turn_id": _active_semantic_turn_id(),
                    "trace_id": _active_semantic_trace_id(),
                },
            )
        )
    _emit_semantic_event(
        app,
        sid,
        "tool.selection.invalid",
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status="failed",
        summary=summary,
        actor={"agent_id": agent_def.id, "role": "expert"},
        subject={"requested_tool": requested_tool},
        blueprint={
            "source": agent_def.source,
            "agent_id": agent_def.id,
            "parent_id": agent_def.parent_id,
            "tier": agent_def.tier,
        },
        provider={
            "default_provider": agent_def.default_provider,
            "default_model": agent_def.default_model,
        },
        payload=payload,
    )


__all__ = ["_emit_invalid_tool_selection_event", "_invalid_tool_selection_from_exception"]
