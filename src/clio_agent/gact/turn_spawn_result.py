"""Result projection helpers for completed child-agent turns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def message_text(message: Any) -> str:
    """Join text parts from a model message in their emitted order."""

    out: list[str] = []
    for part in getattr(message, "parts", None) or []:
        text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
        part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
        if part_type == "text":
            out.append(str(text or ""))
    return "".join(out).strip()


def final_assistant_message(messages: Any) -> Any | None:
    """Return the newest model answer, excluding appended compaction checkpoints."""

    candidates = []
    for message in messages or []:
        metadata = getattr(message, "metadata", {}) or {}
        parts = getattr(message, "parts", None) or []
        is_checkpoint = any(
            (part.get("type") if isinstance(part, dict) else getattr(part, "type", ""))
            == "compaction"
            for part in parts
        )
        if getattr(message, "role", "") == "assistant" and not metadata.get("live"):
            if not is_checkpoint:
                candidates.append(message)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda message: (
            str(getattr(message, "created_at", "") or ""),
            str(getattr(message, "updated_at", "") or ""),
            str(getattr(message, "id", "") or ""),
        ),
    )


def child_workflow_state(app: "FastAPI", child_sid: str, final: Any) -> dict[str, Any]:
    """Return the workflow state carried by a child result, or an empty mapping."""

    final_metadata = getattr(final, "metadata", {}) or {}
    workflow_state = final_metadata.get("workflow_state")
    if isinstance(workflow_state, dict):
        return workflow_state
    child_session = app.state.sessions.get(child_sid)
    session_metadata = getattr(child_session, "metadata", {}) or {}
    workflow_state = session_metadata.get("workflow_state")
    return workflow_state if isinstance(workflow_state, dict) else {}
