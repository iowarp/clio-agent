"""Derive acceptance from actual model-visible tool Parts, never from prose claims."""

from __future__ import annotations

from typing import Any


def model_task_evidence(messages: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Require acceptance and a later model call observing that exact running handle."""
    parts = [part for message in messages for part in message.get("parts", [])]
    accepted = []
    running_observations = []
    for position, part in enumerate(parts):
        if (
            kind == "Subagent"
            and part.get("type") == "expert_handoff"
            and part.get("stage") == "delegate.started"
        ):
            accepted.append(
                {
                    "handle": part["handle_id"],
                    "position": position,
                    "invocation_id": part.get("call_id"),
                    "observation": part,
                }
            )
        if part.get("type") != "tool_result":
            continue
        result = part.get("structured_content") or part.get("metadata", {}).get("result") or {}
        if not isinstance(result, dict):
            continue
        data = result.get("structuredContent", result)
        if not isinstance(data, dict):
            continue
        if data.get("accepted") and data.get("kind") == kind and data.get("handle"):
            accepted.append(
                {
                    "handle": data["handle"],
                    "position": position,
                    "invocation_id": part.get("call_id"),
                    "observation": data,
                }
            )
        if part.get("tool_name") in {"query_tasks", "observe_tasks"}:
            for task in data.get("tasks", []):
                if task.get("task_kind") == kind and task.get("effective_status") not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                }:
                    running_observations.append(
                        {
                            "handle": task.get("handle"),
                            "position": position,
                            "invocation_id": part.get("call_id"),
                            "snapshot": task,
                        }
                    )
    overlap = [
        {"acceptance": receipt, "later_model_observation": observation}
        for receipt in accepted
        for observation in running_observations
        if receipt["handle"] == observation["handle"]
        and receipt["position"] < observation["position"]
    ]
    return {"accepted": accepted, "overlap": overlap, "pass": bool(overlap)}
