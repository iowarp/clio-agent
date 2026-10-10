"""Describe declared task acceptance consistently across MCP tool bridges."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _field(value: Any, name: str, alias: str = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, value.get(alias))
    return getattr(value, name, None)


def mcp_task_description(tool: Any, *, fallback: str = "") -> str:
    """Explain per-tool task support without promoting ordinary server tools."""
    description = str(_field(tool, "description") or fallback or _field(tool, "name") or "MCP tool")
    support = _field(_field(tool, "execution"), "task_support", "taskSupport")
    if not isinstance(support, str) or support not in {"optional", "required"}:
        return description
    guidance = (
        "In a CLIO task-enabled conversation, this MCP task tool returns a durable task handle "
        "as soon as work is accepted, before completion. Keep the handle and continue independent "
        "work. Use query_tasks, observe_tasks, wait_tasks, get_task_result and cancel_tasks "
        "with that handle. Results arrive at the next model iteration or wake an idle agent; "
        "Conversation Stop leaves accepted tasks running."
    )
    if support == "optional":
        guidance += " This tool may instead return an ordinary result directly."
    return description if guidance in description else f"{description}\n\n{guidance}"
