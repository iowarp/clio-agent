"""Message and transcript projections for GACT 0.3."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from clio_agent.gact.a2ui import project_a2ui_parts
from clio_agent.gact.protocol.v3 import utcnow_iso


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a typed mapping view for untrusted wire values."""

    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    """Return a typed list view for untrusted wire values."""

    return value if isinstance(value, list) else []


def _action_cards(part: Mapping[str, Any]) -> list[dict[str, Any]]:
    actions = part.get("actions")
    if not isinstance(actions, list):
        return []
    projected: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, Mapping) or not action.get("id"):
            continue
        behavior = action.get("behavior")
        projected_behavior: dict[str, Any] = {
            "kind": str(behavior.get("kind") or "unavailable")
            if isinstance(behavior, Mapping)
            else "unavailable"
        }
        if isinstance(behavior, Mapping):
            if behavior.get("handle_id"):
                projected_behavior["handle_id"] = str(behavior["handle_id"])
            if behavior.get("reason"):
                projected_behavior["reason"] = str(behavior["reason"])
        projected.append(
            {
                "id": str(action["id"]),
                "label": str(action.get("label") or action["id"]),
                "enabled": bool(action.get("enabled", True)),
                "behavior": projected_behavior,
            }
        )
    return projected


def _resource_delivery(metadata: Mapping[str, Any]) -> dict[str, str] | None:
    """Project the persisted delivery decision without exposing private metadata."""

    raw = metadata.get("delivery")
    if not isinstance(raw, Mapping):
        return None
    representation = str(raw.get("representation") or "")
    if representation not in {
        "native",
        "bounded_tools",
        "structured_document",
        "sandbox",
        "retrieval",
        "metadata_only",
    }:
        return None
    delivery = {"representation": representation}
    for key in ("evidence_source", "reason"):
        value = str(raw.get(key) or "")
        if value:
            delivery[key] = value
    return delivery


def part_to_v3_block(part: Mapping[str, Any]) -> dict[str, Any]:
    """Project one GACT part into a CLIO message block.

    Unknown structured parts remain visible as labeled routing metadata rather
    than silently disappearing or masquerading as prose.
    """

    part_id = str(part.get("id") or "")
    part_type = str(part.get("type") or "unknown")
    metadata = _mapping(part.get("metadata"))
    common: dict[str, Any] = {}
    if part.get("agent_id"):
        common["agent_id"] = str(part["agent_id"])
    if isinstance(part.get("sequence"), int) and part["sequence"] > 0:
        common["sequence"] = int(part["sequence"])
    if metadata.get("stream_source"):
        common["stream_source"] = str(metadata["stream_source"])
    if metadata.get("signature_field_name"):
        common["channel"] = str(metadata["signature_field_name"])
    elif part_type == "text":
        # Text parts are the model's visible answer channel unless the provider
        # bridge explicitly identifies a richer signature field.  Keeping this
        # server-owned means consumers never infer channel semantics by looking
        # ahead for a tool call or comparing prose.
        common["channel"] = "answer"
    if part_type == "text":
        return {
            "id": part_id,
            "type": "text",
            "text": str(part.get("text") or ""),
            **common,
        }
    if part_type == "thinking":
        return {
            "id": part_id,
            "type": "reasoning",
            "text": str(part.get("text") or ""),
            "source": str(metadata.get("thinking_source") or "provider"),
            **(
                {"provider_source": str(metadata["provider_source"])}
                if metadata.get("provider_source")
                else {}
            ),
            **(
                {"default_collapsed": metadata["default_collapsed"]}
                if isinstance(metadata.get("default_collapsed"), bool)
                else {}
            ),
            **common,
        }
    if part_type in {"tool_call", "tool_result"}:
        return {
            "id": part_id,
            "type": "tool",
            "tool_id": str(part.get("call_id") or part_id),
            **({"thought": str(part["thought"])} if part.get("thought") else {}),
            **common,
        }
    if part_type in {"plan", "compaction"}:
        return {
            "id": part_id,
            "type": "plan",
            "title": str(part.get("title") or "Plan"),
            "detail": str(part.get("summary") or part.get("text") or ""),
            **common,
        }
    if part_type in {"task", "session_task", "task_notification"}:
        return {
            "id": part_id,
            "type": "task",
            "task_id": str(part.get("task_id") or part.get("handle_id") or part_id),
            **common,
        }
    if part_type == "expert_handoff":
        return {
            "id": part_id,
            "type": "subagent",
            "subagent_id": str(part.get("handle_id") or part_id),
            **common,
        }
    if part_type == "resource_link":
        metadata = _mapping(part.get("metadata"))
        return {
            "id": part_id,
            "type": "artifact",
            "artifact_id": str(metadata.get("artifact_id") or part.get("uri") or part_id),
            **common,
        }
    if part_type == "resource_ref":
        metadata = _mapping(part.get("metadata"))
        delivery = _resource_delivery(metadata)
        return {
            "id": part_id,
            "type": "resource",
            "resource_id": str(part.get("resource_id") or ""),
            "resource_revision": str(part.get("resource_revision") or ""),
            "workspace_id": str(metadata.get("workspace_id") or ""),
            "name": str(part.get("name") or "Attachment"),
            "media_type": str(part.get("media_type") or "application/octet-stream"),
            **({"delivery": delivery} if delivery is not None else {}),
            **common,
        }
    if part_type == "action_card":
        return {
            "id": part_id,
            "type": "action_card",
            "title": str(part.get("title") or "Action required"),
            "detail": str(part.get("body") or ""),
            "source": str(part.get("source") or ""),
            "severity": str(part.get("severity") or "info"),
            "status": str(part.get("status") or "active"),
            "actions": _action_cards(part),
            **common,
        }
    if part_type == "a2ui":
        return {
            "id": part_id,
            "type": "a2ui",
            "surface_id": str(part.get("surface_id") or part_id),
            **common,
        }
    if part_type == "file_diff":
        return {
            "id": part_id,
            "type": "diff",
            "path": str(part.get("path") or "Unavailable"),
            "unified_diff": str(part.get("unified_diff") or ""),
            **common,
        }
    if part_type == "error":
        metadata = _mapping(part.get("metadata"))
        return {
            "id": part_id,
            "type": "error",
            "code": str(metadata.get("code") or "agent_error"),
            "message": str(part.get("text") or "CLIO reported an error"),
            "recoverable": bool(metadata.get("recoverable", False)),
            **common,
        }
    if part_type == "routing_decision":
        return {
            "id": part_id,
            "type": "routing",
            "label": str(part.get("selected_agent") or "Routing decision"),
            "detail": str(part.get("rationale") or ""),
            **common,
        }
    text = str(part.get("text") or "")
    return {
        "id": part_id,
        "type": "routing",
        "label": part_type.replace("_", " ").title(),
        **({"detail": text} if text else {}),
        **common,
    }


def message_to_v3(message: Any) -> dict[str, Any]:
    """Project a persisted or live message into ordered CLIO blocks."""

    wire = message.to_wire() if hasattr(message, "to_wire") else dict(message)
    raw_parts = _list(wire.get("parts"))
    role = str(wire.get("role") or "system")
    if role not in {"user", "assistant", "system"}:
        role = "system"
    blocks: list[dict[str, Any]] = []
    tool_blocks: set[str] = set()
    artifact_blocks: set[str] = set()
    for part in raw_parts:
        if not isinstance(part, Mapping):
            continue
        metadata = _mapping(part.get("metadata"))
        if part.get("type") == "a2ui" and metadata.get("projection_only") is True:
            continue
        block = part_to_v3_block(part)
        if block["type"] == "tool":
            tool_id = str(block["tool_id"])
            if tool_id in tool_blocks:
                continue
            tool_blocks.add(tool_id)
        if block["type"] == "artifact":
            artifact_id = str(block["artifact_id"])
            if artifact_id in artifact_blocks:
                continue
            artifact_blocks.add(artifact_id)
        blocks.append(block)
    row: dict[str, Any] = {
        "id": str(wire.get("id") or ""),
        "session_id": str(wire.get("session_id") or ""),
        "role": role,
        "created_at": str(wire.get("created_at") or utcnow_iso()),
        "blocks": blocks,
    }
    turn_id = str(wire.get("turn_id") or "")
    if turn_id:
        row["run_id"] = turn_id
    metadata = _mapping(wire.get("metadata"))
    tokens = _mapping(wire.get("tokens"))
    row["usage"] = {
        "input": int(tokens.get("input") or 0),
        "output": int(tokens.get("output") or 0),
        "cache_read": int(tokens.get("cache_read") or 0),
        "cache_write": int(tokens.get("cache_write") or 0),
    }
    row["cost_usd"] = float(wire.get("cost_usd") or 0.0)
    if wire.get("stop_reason"):
        row["stop_reason"] = str(wire["stop_reason"])
    if isinstance(wire.get("error_info"), Mapping):
        row["error_info"] = dict(wire["error_info"])
    if metadata.get("status") != "running" and wire.get("updated_at"):
        row["completed_at"] = str(wire["updated_at"])
    return row


@dataclass
class _TranscriptProjection:
    session_id: str
    wire: Mapping[str, Any]
    subagent_links: Mapping[str, Mapping[str, Any]]
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    subagents: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)


_EntityProjector = Callable[[_TranscriptProjection, Mapping[str, Any], str], None]


def _project_tool(context: _TranscriptProjection, part: Mapping[str, Any], part_id: str) -> None:
    tool_id = str(part.get("call_id") or part_id)
    current = context.tools.get(tool_id, {})
    failed = bool(part.get("is_error"))
    part_type = str(part.get("type") or "")
    state = "failed" if failed else ("succeeded" if part_type == "tool_result" else "running")
    output = current.get("output")
    if part_type == "tool_result":
        for key in ("structured_content", "content_blocks", "content", "text"):
            value = part.get(key)
            if value is not None and (key not in {"content", "text"} or value):
                output = value
                break
    context.tools[tool_id] = {
        "id": tool_id,
        "session_id": context.session_id,
        **({"run_id": str(context.wire["turn_id"])} if context.wire.get("turn_id") else {}),
        "name": str(part.get("tool_name") or current.get("name") or "Tool"),
        **(
            {"title": str(part["tool_title"])}
            if part.get("tool_title")
            else ({"title": current["title"]} if current.get("title") else {})
        ),
        "state": state,
        "input": current.get("input", part.get("input")),
        "output": output,
        "duration_ms": part.get("duration_ms") or current.get("duration_ms"),
        **({"error": str(part.get("text") or "Tool failed")} if failed else {}),
    }


def _project_task(context: _TranscriptProjection, part: Mapping[str, Any], part_id: str) -> None:
    task_id = str(part.get("task_id") or part.get("handle_id") or part_id)
    context.tasks[task_id] = {
        "id": task_id,
        "session_id": context.session_id,
        "title": str(part.get("title") or part.get("run_label") or "Task"),
        "state": str(part.get("live_state") or "completed"),
        "detail": str(part.get("text") or ""),
    }


def _project_subagent(
    context: _TranscriptProjection, part: Mapping[str, Any], part_id: str
) -> None:
    subagent_id = str(part.get("handle_id") or part_id)
    metadata = _mapping(part.get("metadata"))
    link = context.subagent_links.get(subagent_id, {})
    child_session_id = str(part.get("child_session_id") or link.get("child_session_id") or "")
    agent_id = str(
        part.get("child_agent") or metadata.get("agent_id") or link.get("agent_id") or ""
    )
    result = str(metadata.get("output") or link.get("result") or "")
    summary = str(
        metadata.get("summary") or link.get("summary") or result or part.get("text") or ""
    )
    task = str(metadata.get("question") or link.get("task") or "")
    context.subagents[subagent_id] = {
        "id": subagent_id,
        "session_id": context.session_id,
        **({"parent_run_id": str(context.wire["turn_id"])} if context.wire.get("turn_id") else {}),
        "title": str(part.get("run_label") or link.get("title") or agent_id or "Subagent"),
        "state": str(part.get("live_state") or part.get("status") or "completed"),
        **({"summary": summary} if summary else {}),
        **({"agent_id": agent_id} if agent_id else {}),
        **({"child_session_id": child_session_id} if child_session_id else {}),
        **({"task": task} if task else {}),
        **({"result": result} if result else {}),
        **(
            {"duration_ms": float(part["duration_ms"])}
            if isinstance(part.get("duration_ms"), int | float)
            else {}
        ),
    }


def _project_artifact(
    context: _TranscriptProjection, part: Mapping[str, Any], part_id: str
) -> None:
    metadata = _mapping(part.get("metadata"))
    artifact_id = str(metadata.get("artifact_id") or part.get("uri") or part_id)
    context.artifacts[artifact_id] = {
        "id": artifact_id,
        "session_id": context.session_id,
        "name": str(part.get("name") or Path(artifact_id).name or "Artifact"),
        "media_type": str(
            part.get("mime_type") or part.get("media_type") or "application/octet-stream"
        ),
        "uri": str(part.get("uri") or ""),
        **({"workspace_id": str(metadata["workspace_id"])} if metadata.get("workspace_id") else {}),
        **({"fetch_path": str(metadata["fetch_url"])} if metadata.get("fetch_url") else {}),
        **({"custody": str(metadata["custody"])} if metadata.get("custody") else {}),
        **({"sha256": str(metadata["sha256"])} if metadata.get("sha256") else {}),
        **(
            {"size": int(metadata["size_bytes"])}
            if isinstance(metadata.get("size_bytes"), int)
            else {}
        ),
        "created_at": str(context.wire.get("created_at") or utcnow_iso()),
    }


_ENTITY_PROJECTORS: dict[str, _EntityProjector] = {
    "tool_call": _project_tool,
    "tool_result": _project_tool,
    "task": _project_task,
    "session_task": _project_task,
    "task_notification": _project_task,
    "expert_handoff": _project_subagent,
    "resource_link": _project_artifact,
}


def transcript_entities(
    messages: list[Any],
    session_id: str,
    *,
    subagent_links: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a normalized transcript snapshot and its referenced entities."""

    projected_messages: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    subagents: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    # (part recorded_at or its message created_at, arrival index, part). Callers
    # hand this function rows in their own order -- the v3 transcript route pages
    # newest-first -- but an A2UI fold is only correct in transcript order, so
    # the parts are re-sorted chronologically before they are folded.
    transcript_parts: list[tuple[str, int, Any]] = []

    for message in messages:
        projected = message_to_v3(message)
        projected_messages.append(projected)
        wire = message.to_wire() if hasattr(message, "to_wire") else dict(message)
        context = _TranscriptProjection(
            session_id=session_id,
            wire=wire,
            subagent_links=subagent_links or {},
        )
        raw_parts = _list(wire.get("parts"))
        created_at = str(projected.get("created_at") or wire.get("created_at") or "")
        for raw_part in raw_parts:
            metadata = raw_part.get("metadata") if isinstance(raw_part, Mapping) else None
            recorded_at = str((metadata or {}).get("recorded_at") or created_at)
            transcript_parts.append((recorded_at, len(transcript_parts), raw_part))
        for part in raw_parts:
            if not isinstance(part, Mapping):
                continue
            part_id = str(part.get("id") or "")
            part_type = str(part.get("type") or "")
            projector = _ENTITY_PROJECTORS.get(part_type)
            if projector is not None:
                projector(context, part, part_id)

        tools.update(context.tools)
        tasks.update(context.tasks)
        subagents.update(context.subagents)
        artifacts.update(context.artifacts)

    # A child task is an authoritative relation even when the parent has not yet
    # persisted an expert_handoff part (for example, while detached work is still
    # running).  Project that relation on the server so clients do not mint fake
    # messages, processes, or subagent entities from the sessions list.
    for subagent_id, link in (subagent_links or {}).items():
        if subagent_id in subagents:
            continue
        child_session_id = str(link.get("child_session_id") or "")
        agent_id = str(link.get("agent_id") or "")
        summary = str(link.get("summary") or link.get("result") or "")
        task = str(link.get("task") or "")
        result = str(link.get("result") or "")
        created_at = str(link.get("created_at") or utcnow_iso())
        subagents[subagent_id] = {
            "id": subagent_id,
            "session_id": session_id,
            **({"parent_run_id": str(link["parent_run_id"])} if link.get("parent_run_id") else {}),
            "title": str(link.get("title") or agent_id or "Subagent"),
            "state": str(link.get("state") or "interrupted"),
            **({"child_session_id": child_session_id} if child_session_id else {}),
            **({"agent_id": agent_id} if agent_id else {}),
            **({"summary": summary} if summary else {}),
            **({"task": task} if task else {}),
            **({"result": result} if result else {}),
            **(
                {"duration_ms": float(link["duration_ms"])}
                if isinstance(link.get("duration_ms"), int | float)
                else {}
            ),
        }
        projected_messages.append(
            {
                "id": f"child-relation:{subagent_id}",
                "session_id": session_id,
                **({"run_id": str(link["parent_run_id"])} if link.get("parent_run_id") else {}),
                "role": "system",
                "created_at": created_at,
                "blocks": [
                    {
                        "id": f"child-relation-block:{subagent_id}",
                        "type": "subagent",
                        "subagent_id": subagent_id,
                    }
                ],
                "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                "cost_usd": 0.0,
            }
        )

    projected_messages.sort(key=lambda message: str(message.get("created_at") or ""))

    transcript_parts.sort(key=lambda row: (row[0], row[1]))
    surface_records, a2ui_degradations = project_a2ui_parts(
        [row[2] for row in transcript_parts], session_id
    )
    surfaces = list(surface_records.values())
    surfaces.sort(key=lambda row: row.created_at)

    return {
        "messages": projected_messages,
        "tools": list(tools.values()),
        "tasks": list(tasks.values()),
        "subagents": list(subagents.values()),
        "artifacts": list(artifacts.values()),
        "surfaces": [surface.to_wire() for surface in surfaces],
        "a2ui_degradations": a2ui_degradations,
    }
