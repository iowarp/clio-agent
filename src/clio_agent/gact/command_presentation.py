"""Service command receipts, including Goal notices distinct from model answers."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.parts import Part


def _receipt(body: str) -> tuple[str, str]:
    if body.startswith("Goal started."):
        return "goal_started", ""
    if body == "Goal cleared.":
        return "goal_stopped", ""
    if body.startswith("/goal rejected:"):
        return "goal_rejected", body.removeprefix("/goal rejected:").strip()
    return "goal_command", body


def _goal_command_part(app: Any, sid: str, body: str) -> Part:
    """Build a service-owned Goal notice from the command's retained result."""
    from clio_agent.gact.goal import _get_goal  # noqa: PLC0415

    code, text = _receipt(body)
    goal = _get_goal(app, sid)
    if code in {"goal_started", "goal_stopped"}:
        text = str(goal.get("condition") or "")
    return Part(
        id=f"part_goal_{uuid.uuid4().hex[:10]}",
        type="notice",
        source="goal",
        code=code,
        agent_id="main",
        text=text,
        metadata={"synthetic": "command_result", "command": "/goal"},
    )


def command_result_part(app: Any, sid: str, command: str, body: str) -> Part:
    """Build the retained result of an orchestrator-owned service command."""
    if command == "/goal":
        return _goal_command_part(app, sid, body)
    return Part(
        id=f"part_cmd_{uuid.uuid4().hex[:10]}",
        type="text",
        agent_id="main",
        metadata={"synthetic": "command_result", "command": command},
        text=f"[{command}] {body}",
    )


def legacy_goal_receipt(part: Mapping[str, Any]) -> Mapping[str, Any]:
    """Project old typed command receipts without rewriting stored history.

    Only server command-result metadata selects this presentation; ordinary prose
    mentioning /goal keeps its original text and semantics.
    """
    metadata = part.get("metadata")
    if not (
        part.get("type") == "text"
        and isinstance(metadata, Mapping)
        and metadata.get("synthetic") == "command_result"
        and metadata.get("command") == "/goal"
    ):
        return part
    body = str(part.get("text") or "").removeprefix("[/goal] ")
    code, text = _receipt(body)
    return {**part, "type": "notice", "source": "goal", "code": code, "text": text}
