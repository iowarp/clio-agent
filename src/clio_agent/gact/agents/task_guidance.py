"""Shared task lifecycle guidance at the common provider request boundary."""

from __future__ import annotations

from collections.abc import Iterable

from clio_agent.prompts import PromptRegistry


def task_guidance(tool_names: Iterable[str]) -> str:
    """Resolve editable task instructions when the agent has shared task controls."""
    if not {"query_tasks", "wait_tasks"}.issubset(tool_names):
        return ""
    prompt = PromptRegistry().resolve("clio.runtime.tasks")
    if prompt is None or not prompt.text.strip() or prompt.validation_errors:
        raise ValueError("The shared task lifecycle prompt is unavailable")
    return prompt.text.strip()
