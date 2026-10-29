"""Bounded public conversation origins, independent of mutable parent transcripts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FORK_ORIGIN_KEY = "fork_origin"


def saved_delegation_origin(
    metadata: Mapping[str, Any], *, session_id: str, parent_session_id: str
) -> dict[str, str] | None:
    """Expose only the recorded owner turn and launch time, never the task briefing."""
    task = metadata.get("agent_task")
    if not isinstance(task, Mapping):
        return None
    if (
        not parent_session_id
        or task.get("parent_session_id") != parent_session_id
        or task.get("child_session_id") != session_id
        or not isinstance(task.get("parent_turn_id"), str)
        or not task["parent_turn_id"]
        or not isinstance(task.get("created_at"), str)
        or not task["created_at"]
    ):
        return None
    return {
        "session_id": parent_session_id,
        "turn_id": task["parent_turn_id"],
        "created_at": task["created_at"],
    }


def saved_fork_origin(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return only valid public checkpoint fields from stored session metadata."""
    value = metadata.get(FORK_ORIGIN_KEY)
    if not isinstance(value, Mapping):
        return None
    source = value.get("session_id")
    message = value.get("message_id")
    count = value.get("message_count")
    created = value.get("created_at")
    if (
        not isinstance(source, str)
        or not source
        or not isinstance(message, str)
        or type(count) is not int
        or count < 0
        or bool(message) != bool(count)
        or not isinstance(created, str)
        or not created
    ):
        return None
    result = {
        "session_id": source,
        "message_id": message,
        "message_count": count,
        "created_at": created,
    }
    title = value.get("title")
    if isinstance(title, str) and title:
        result["title"] = title
    # An agent's fork names its author, the fork call and the note it gave.
    author = value.get("author")
    if isinstance(author, Mapping) and author.get("kind") in {"agent", "human"}:
        result["author"] = {k: str(v) for k, v in author.items() if isinstance(v, str)}
    for key in ("note", "invocation"):
        if isinstance(value.get(key), str) and value[key]:
            result[key] = value[key]
    return result
