"""Typed child-turn failure projection."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def _error_field(error_info: Any, field: str, default: Any = None) -> Any:
    if not error_info:
        return default
    if isinstance(error_info, dict):
        return error_info.get(field, default)
    return getattr(error_info, field, default)


def child_task_error_reason(error_info: Any) -> str:
    """Project a child turn's typed cause onto the AgentTask vocabulary."""

    from clio_agent.gact.agent_tasks import ERROR_REASONS  # noqa: PLC0415

    details = _error_field(error_info, "details", {})
    declared = str(details.get("reason") or "") if isinstance(details, dict) else ""
    return declared if declared in ERROR_REASONS else "agent_error"


def child_task_failure_result(
    app: "FastAPI",
    child_sid: str,
    final: Any,
    *,
    workflow_state: Callable[["FastAPI", str, Any], dict[str, Any]],
    excerpt_limit: int,
) -> dict[str, Any]:
    """Retain typed child failure detail for parent and UI observability."""

    error_info = getattr(final, "error_info", None)
    message = str(_error_field(error_info, "message", "") or "").strip()
    return {
        "message_ref": str(getattr(final, "id", "") or ""),
        "answer_excerpt": message[:excerpt_limit],
        "workflow_state": workflow_state(app, child_sid, final),
    }
