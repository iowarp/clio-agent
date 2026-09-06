"""Uniform model-context rendering for completed agent-task notifications."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_task_artifacts import artifact_context_for_task, artifact_context_text

if TYPE_CHECKING:
    from fastapi import FastAPI

_NOTIFY_EXCERPT_MAX = 600


def _sanitize_excerpt(text: str, marker: str) -> str:
    """Neutralize child-authored structural tokens in a fenced result excerpt."""

    text = re.sub(r"`{3,}", "``", text)
    return text.replace(marker, "[marker removed]")


def compose_task_notification(task: Any, *, app: "FastAPI | None", marker: str) -> str:
    """Compose one uniform, bounded terminal-task context block."""

    result = task.result or {}
    excerpt = _sanitize_excerpt(str(result.get("answer_excerpt", ""))[:_NOTIFY_EXCERPT_MAX], marker)
    artifact_block = ""
    if app is not None and task.artifact_ref:
        artifact_block = "\n" + artifact_context_text(artifact_context_for_task(app, task)).replace(
            marker, "[marker removed]"
        )
    return (
        f"### task {task.task_id} — {task.agent_ref.get('expert_id', '')} [{task.status}]\n"
        f"- child_session_id: {task.child_session_id}\n"
        f"- error_reason: {task.error_reason}\n"
        f"- result_excerpt:\n```\n{excerpt}\n```{artifact_block}"
    )


__all__ = ["compose_task_notification"]
