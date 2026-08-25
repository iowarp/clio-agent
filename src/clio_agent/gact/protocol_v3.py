"""GACT 0.3 projections for the React workspace.

The running server keeps its 0.2 models and routes for established clients.
Requests that explicitly advertise ``X-GACT-Version: 0.3`` receive these
normalized projections instead.  Keeping the conversion pure makes protocol
truth testable without coupling domain producers to a particular UI.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import Request

from clio_agent import __version__ as clio_agent_version
from clio_agent.gact.events import Event
from clio_agent.gact.providers.config import _effective_lm_config
from clio_agent.gact.workspaces import workspace_display_name

GACT_V3 = "0.3"
A2UI_V091 = "0.9.1"
CLIO_A2UI_CATALOG_ID = "https://iowarp.ai/a2ui/catalogs/clio-workspace/v1"
CONNECTION_ID = "local"


def utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp for protocol provenance."""

    return datetime.now(timezone.utc).isoformat()


def requests_gact_v3(request: Request) -> bool:
    """Return whether a request explicitly negotiated GACT 0.3."""

    return request.headers.get("x-gact-version", "").strip() == GACT_V3


def workspace_to_v3(workspace: Any) -> dict[str, Any]:
    """Project a workspace without promoting its full path into its label."""

    root_path = str(getattr(workspace, "root_path", "") or "")
    name = str(getattr(workspace, "name", "") or "")
    metadata = getattr(workspace, "metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    workspace_id = str(getattr(workspace, "id", "") or "")
    display_name = workspace_display_name(
        workspace_id=workspace_id,
        name=name,
        root_path=root_path,
        metadata=metadata,
        configured_display_name=str(getattr(workspace, "display_name", "") or ""),
    )
    config = getattr(workspace, "config", {})
    if not isinstance(config, dict):
        config = {}
    granted_roots = [
        str(value)
        for value in config.get("granted_write_roots", []) or []
        if str(value).strip() and str(value) != root_path
    ]
    source_folders = (
        [{"path": root_path, "name": Path(root_path).name or root_path, "primary": True}]
        if root_path
        else []
    )
    source_folders.extend(
        {"path": path, "name": Path(path).name or path, "primary": False}
        for path in dict.fromkeys(granted_roots)
    )
    return {
        "id": workspace_id,
        "name": name or display_name,
        "display_name": display_name,
        "path": root_path,
        "connection_id": str(getattr(workspace, "connection_id", "") or CONNECTION_ID),
        "pinned": bool(metadata.get("pinned", False)),
        "source_folders": source_folders,
    }


_SESSION_STATE = {
    "idle": "completed",
    "running": "running",
    "waiting_permission": "waiting_permission",
    "waiting_user": "waiting_user",
    "error": "failed",
    "cancelled": "cancelled",
}

_LIVE_WORK_STATE = {
    "queued": "queued",
    "running": "running",
    "working": "running",
    "input_required": "waiting_user",
    "waiting_permission": "waiting_permission",
    "waiting_user": "waiting_user",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}


def session_to_v3(session: Any) -> dict[str, Any]:
    """Project the 0.2 session record into the normalized 0.3 shape."""

    model = getattr(session, "model", {})
    if not isinstance(model, Mapping):
        model = {}
    metadata = getattr(session, "metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    agent = getattr(session, "agent", {})
    if isinstance(agent, Mapping):
        agent_id = agent.get("id")
    else:
        agent_id = getattr(agent, "id", "")
    status = str(getattr(session, "status", "") or "idle")
    row: dict[str, Any] = {
        "id": str(getattr(session, "id", "") or ""),
        "workspace_id": str(getattr(session, "workspace_id", "") or ""),
        "title": str(getattr(session, "title", "") or "Untitled session"),
        "state": _SESSION_STATE.get(status, "interrupted"),
        "created_at": str(getattr(session, "created_at", "") or utcnow_iso()),
        "updated_at": str(getattr(session, "updated_at", "") or utcnow_iso()),
        "last_interaction_at": str(
            getattr(session, "last_interaction_at", "")
            or getattr(session, "created_at", "")
            or utcnow_iso()
        ),
        "pinned": bool(metadata.get("pinned", False)),
        "archived": bool(getattr(session, "archived", False)),
    }
    optional = {
        "provider_id": model.get("provider_id"),
        "model_id": model.get("model_id"),
        "effort": metadata.get("effort") or metadata.get("thinking_level"),
        "branch": metadata.get("branch") or metadata.get("git_branch"),
        "parent_session_id": getattr(session, "parent_session_id", ""),
        "agent_id": agent_id,
        "active_blueprint_id": metadata.get("active_agent_blueprint_id"),
        "active_blueprint_name": metadata.get("active_agent_blueprint_name"),
        "active_blueprint_version": metadata.get("active_agent_blueprint_version"),
        "active_blueprint_scope": metadata.get("active_agent_blueprint_scope"),
    }
    row.update({key: str(value) for key, value in optional.items() if value})
    row.update(
        {
            "mode": str(getattr(session, "mode", "edit") or "edit"),
            "edit_mode": str(getattr(session, "edit_mode", "diff") or "diff"),
            "routing_mode": str(getattr(session, "routing_mode", "auto") or "auto"),
            "approval_mode": str(getattr(session, "approval_mode", "ask") or "ask"),
        }
    )
    return row


def capabilities_to_v3(app: Any, flags: Any, *, replay_retention: int) -> dict[str, Any]:
    """Build the explicit 0.3 negotiation response from live server truth."""

    raw_flags = flags.model_dump() if hasattr(flags, "model_dump") else dict(flags)
    capabilities = {
        key: value
        for key, value in raw_flags.items()
        if isinstance(value, bool) and not key.startswith("x_")
    }
    capabilities.update(
        {
            "a2ui": True,
            "replay": True,
            "workspace_display_names": True,
            "scoped_events": True,
        }
    )
    degradations: list[dict[str, Any]] = []
    for key, value in capabilities.items():
        if not value:
            degradations.append(
                {
                    "code": "capability_unavailable",
                    "reason": f"The server does not provide {key.replace('_', ' ')}.",
                    "capability": key,
                    "recoverable": False,
                }
            )

    lm_config = _effective_lm_config(app)
    lm_status = getattr(app.state, "lm_config_status", {})
    configured = isinstance(lm_config, Mapping) and bool(
        lm_config.get("provider") and lm_config.get("model")
    )
    failed_reason = ""
    if isinstance(lm_status, Mapping) and lm_status.get("state") == "error":
        failed_reason = str(lm_status.get("error") or lm_status.get("status_message") or "")
    observed_at = utcnow_iso()
    model_catalog = {
        "source": "provider" if configured else "unavailable",
        "observed_at": observed_at,
        "stale": bool(failed_reason) or not configured,
        **(
            {"reason": failed_reason}
            if failed_reason
            else (
                {}
                if configured
                else {"reason": "No active provider model catalog has been observed."}
            )
        ),
    }
    if not configured:
        degradations.append(
            {
                "code": "model_catalog_unavailable",
                "reason": str(model_catalog["reason"]),
                "capability": "providers",
                "recoverable": True,
            }
        )
    return {
        "service": {"name": "clio-agent", "version": clio_agent_version},
        "gact_versions": [GACT_V3, "0.2"],
        "a2ui_versions": [A2UI_V091],
        "replay": {"supported": True, "retention": replay_retention},
        "capabilities": capabilities,
        "degradations": degradations,
        "model_catalog": model_catalog,
        **(
            {
                "active_model": {
                    "provider_id": str(lm_config["provider"]),
                    "model_id": str(lm_config["model"]),
                    **(
                        {"effort": str(lm_config["thinking_level"])}
                        if lm_config.get("thinking_level")
                        else {}
                    ),
                }
            }
            if configured
            else {}
        ),
    }


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


def part_to_v3_block(part: Mapping[str, Any]) -> dict[str, Any]:
    """Project one GACT part into a CLIO message block.

    Unknown structured parts remain visible as labeled routing metadata rather
    than silently disappearing or masquerading as prose.
    """

    part_id = str(part.get("id") or "")
    part_type = str(part.get("type") or "unknown")
    metadata = part.get("metadata") if isinstance(part.get("metadata"), Mapping) else {}
    common: dict[str, Any] = {}
    if part.get("agent_id"):
        common["agent_id"] = str(part["agent_id"])
    if isinstance(part.get("sequence"), int) and part["sequence"] > 0:
        common["sequence"] = int(part["sequence"])
    if metadata.get("stream_source"):
        common["stream_source"] = str(metadata["stream_source"])
    if metadata.get("signature_field_name"):
        common["channel"] = str(metadata["signature_field_name"])
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
        metadata = part.get("metadata") if isinstance(part.get("metadata"), Mapping) else {}
        return {
            "id": part_id,
            "type": "artifact",
            "artifact_id": str(metadata.get("artifact_id") or part.get("uri") or part_id),
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
        metadata = part.get("metadata") if isinstance(part.get("metadata"), Mapping) else {}
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


def _reasoning_calls(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project the server-captured per-model-call reasoning ledger without inventing parts."""

    raw_rows = metadata.get("reasoning_log")
    if not isinstance(raw_rows, list):
        return []
    calls: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping) or not raw.get("reasoning"):
            continue
        reasoning = str(raw["reasoning"])
        calls.append(
            {
                "id": f"reasoning_call_{index + 1}",
                "reasoning": reasoning,
                "reasoning_chars": (
                    int(raw["reasoning_chars"])
                    if isinstance(raw.get("reasoning_chars"), int)
                    else len(reasoning)
                ),
                **({"model": str(raw["model"])} if raw.get("model") else {}),
                **({"question": str(raw["question"])} if raw.get("question") else {}),
                **({"response": str(raw["response"])} if raw.get("response") else {}),
                **({"timestamp": str(raw["timestamp"])} if raw.get("timestamp") else {}),
            }
        )
    return calls


def message_to_v3(message: Any) -> dict[str, Any]:
    """Project a persisted or live message into ordered CLIO blocks."""

    wire = message.to_wire() if hasattr(message, "to_wire") else dict(message)
    raw_parts = wire.get("parts") if isinstance(wire.get("parts"), list) else []
    role = str(wire.get("role") or "system")
    if role not in {"user", "assistant", "system"}:
        role = "system"
    blocks: list[dict[str, Any]] = []
    tool_blocks: set[str] = set()
    for part in raw_parts:
        if not isinstance(part, Mapping):
            continue
        block = part_to_v3_block(part)
        if block["type"] == "tool":
            tool_id = str(block["tool_id"])
            if tool_id in tool_blocks:
                continue
            tool_blocks.add(tool_id)
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
    metadata = wire.get("metadata") if isinstance(wire.get("metadata"), Mapping) else {}
    tokens = wire.get("tokens") if isinstance(wire.get("tokens"), Mapping) else {}
    reasoning_calls = _reasoning_calls(metadata)
    row["usage"] = {
        "input": int(tokens.get("input") or 0),
        "output": int(tokens.get("output") or 0),
        "cache_read": int(tokens.get("cache_read") or 0),
        "cache_write": int(tokens.get("cache_write") or 0),
    }
    row["cost_usd"] = float(wire.get("cost_usd") or 0.0)
    if wire.get("stop_reason"):
        row["stop_reason"] = str(wire["stop_reason"])
    if reasoning_calls:
        row["reasoning_calls"] = reasoning_calls
    if isinstance(wire.get("error_info"), Mapping):
        row["error_info"] = dict(wire["error_info"])
    if metadata.get("status") != "running" and wire.get("updated_at"):
        row["completed_at"] = str(wire["updated_at"])
    return row


def transcript_entities(
    messages: list[Any],
    session_id: str,
    *,
    subagent_links: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Return a normalized transcript snapshot and its referenced entities."""

    projected_messages: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    subagents: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}

    for message in messages:
        projected = message_to_v3(message)
        projected_messages.append(projected)
        wire = message.to_wire() if hasattr(message, "to_wire") else dict(message)
        raw_parts = wire.get("parts") if isinstance(wire.get("parts"), list) else []
        for part in raw_parts:
            if not isinstance(part, Mapping):
                continue
            part_id = str(part.get("id") or "")
            part_type = str(part.get("type") or "")
            if part_type in {"tool_call", "tool_result"}:
                tool_id = str(part.get("call_id") or part_id)
                current = tools.get(tool_id, {})
                failed = bool(part.get("is_error"))
                state = (
                    "failed"
                    if failed
                    else ("succeeded" if part_type == "tool_result" else "running")
                )
                output = current.get("output")
                if part_type == "tool_result":
                    if part.get("structured_content") is not None:
                        output = part.get("structured_content")
                    elif part.get("content_blocks") is not None:
                        output = part.get("content_blocks")
                    elif part.get("content"):
                        output = part.get("content")
                    elif part.get("text"):
                        output = part.get("text")
                tools[tool_id] = {
                    "id": tool_id,
                    "session_id": session_id,
                    "run_id": str(wire.get("turn_id") or "") or None,
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
            elif part_type in {"task", "session_task", "task_notification"}:
                task_id = str(part.get("task_id") or part.get("handle_id") or part_id)
                tasks[task_id] = {
                    "id": task_id,
                    "session_id": session_id,
                    "title": str(part.get("title") or part.get("run_label") or "Task"),
                    "state": str(part.get("live_state") or "completed"),
                    "detail": str(part.get("text") or ""),
                }
            elif part_type == "expert_handoff":
                subagent_id = str(part.get("handle_id") or part_id)
                metadata = part.get("metadata") if isinstance(part.get("metadata"), Mapping) else {}
                link = (subagent_links or {}).get(subagent_id, {})
                child_session_id = str(
                    part.get("child_session_id") or link.get("child_session_id") or ""
                )
                agent_id = str(
                    part.get("child_agent")
                    or metadata.get("agent_id")
                    or link.get("agent_id")
                    or ""
                )
                subagents[subagent_id] = {
                    "id": subagent_id,
                    "session_id": session_id,
                    "parent_run_id": str(wire.get("turn_id") or "") or None,
                    "title": str(
                        part.get("run_label") or link.get("title") or agent_id or "Subagent"
                    ),
                    "state": str(part.get("live_state") or part.get("status") or "completed"),
                    "summary": str(part.get("text") or ""),
                    **({"agent_id": agent_id} if agent_id else {}),
                    **({"child_session_id": child_session_id} if child_session_id else {}),
                    **({"task": str(metadata["question"])} if metadata.get("question") else {}),
                    **({"result": str(metadata["output"])} if metadata.get("output") else {}),
                    **(
                        {"duration_ms": float(part["duration_ms"])}
                        if isinstance(part.get("duration_ms"), int | float)
                        else {}
                    ),
                }
            elif part_type == "resource_link":
                metadata = part.get("metadata") if isinstance(part.get("metadata"), Mapping) else {}
                artifact_id = str(metadata.get("artifact_id") or part.get("uri") or part_id)
                artifacts[artifact_id] = {
                    "id": artifact_id,
                    "session_id": session_id,
                    "name": str(part.get("name") or Path(artifact_id).name or "Artifact"),
                    "media_type": str(
                        part.get("mime_type")
                        or part.get("media_type")
                        or "application/octet-stream"
                    ),
                    "uri": str(part.get("uri") or ""),
                    **(
                        {"workspace_id": str(metadata["workspace_id"])}
                        if metadata.get("workspace_id")
                        else {}
                    ),
                    **(
                        {"fetch_path": str(metadata["fetch_url"])}
                        if metadata.get("fetch_url")
                        else {}
                    ),
                    **({"custody": str(metadata["custody"])} if metadata.get("custody") else {}),
                    **({"sha256": str(metadata["sha256"])} if metadata.get("sha256") else {}),
                    **(
                        {"size": int(metadata["size_bytes"])}
                        if isinstance(metadata.get("size_bytes"), int)
                        else {}
                    ),
                    "created_at": str(wire.get("created_at") or utcnow_iso()),
                }

    return {
        "messages": projected_messages,
        "tools": list(tools.values()),
        "tasks": list(tasks.values()),
        "subagents": list(subagents.values()),
        "artifacts": list(artifacts.values()),
        "surfaces": [],
    }


def _event_identity(event_type: str, payload: Mapping[str, Any]) -> str | None:
    keys_by_type = {
        "session": ("session_id",),
        "message": ("message_id", "id"),
        "tool": ("call_id",),
        "permission": ("permission_id", "id"),
        "task": ("task_id", "handle_id", "id"),
        "artifact": ("artifact_id", "uri", "id"),
        "a2ui": ("surface_id", "id"),
    }
    family = event_type.split(".", 1)[0]
    for key in keys_by_type.get(family, ("id",)):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def event_to_v3(event: Event, *, session: Any = None, workspace_id: str = "") -> dict[str, Any]:
    """Translate a live 0.2 event into the canonical scoped 0.3 envelope."""

    payload: dict[str, Any] = dict(event.payload)
    event_type = event.type
    entity_id = _event_identity(event_type, payload)

    if event_type == "server.connected":
        event_type = "stream.live"
    elif event_type == "session.snapshot" and session is not None:
        event_type = "session.upserted"
        payload = session_to_v3(session)
        entity_id = payload["id"]
    elif event_type in {"session.status_changed", "session.updated"} and session is not None:
        event_type = "session.upserted"
        payload = session_to_v3(session)
        entity_id = payload["id"]
    elif event_type == "message.created":
        event_type = "message.upserted"
        payload = message_to_v3(payload)
        entity_id = payload["id"]
    elif event_type in {"message.part.added", "message.part.updated"}:
        event_type = "message.block.upserted"
        raw_part = payload.get("part")
        payload = {
            "message_id": str(payload.get("message_id") or ""),
            "block": part_to_v3_block(raw_part if isinstance(raw_part, Mapping) else {}),
        }
        entity_id = str(payload["block"].get("id") or "")
    elif event_type == "message.part.delta":
        event_type = "message.block.delta"
        raw_delta = payload.get("delta")
        delta = raw_delta.get("text_append") if isinstance(raw_delta, Mapping) else raw_delta
        payload = {
            "message_id": str(payload.get("message_id") or ""),
            "block_id": str(payload.get("part_id") or ""),
            "delta": str(delta or ""),
        }
        entity_id = payload["block_id"]
    elif event_type == "message.part.completed":
        event_type = "message.block.completed"
        payload = {
            "message_id": str(payload.get("message_id") or ""),
            "block_id": str(payload.get("part_id") or ""),
            "text": str(payload.get("final_text") or ""),
        }
        entity_id = payload["block_id"]
    elif event_type == "message.completed":
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        reasoning_calls = _reasoning_calls(metadata)
        tokens = payload.get("tokens") if isinstance(payload.get("tokens"), Mapping) else {}
        payload = {
            "message_id": str(payload.get("message_id") or ""),
            "completed_at": event.occurred_at,
            "stop_reason": str(payload.get("stop_reason") or "end_turn"),
            "tokens": {
                "input": int(tokens.get("input") or 0),
                "output": int(tokens.get("output") or 0),
                "cache_read": int(tokens.get("cache_read") or 0),
                "cache_write": int(tokens.get("cache_write") or 0),
            },
            "cost_usd": payload.get("cost_usd"),
            **({"reasoning_calls": reasoning_calls} if reasoning_calls else {}),
            **({"error_info": payload["error_info"]} if payload.get("error_info") else {}),
        }
        entity_id = payload["message_id"]
    elif event_type == "tool.call.started":
        event_type = "tool.upserted"
        entity_id = str(payload.get("call_id") or "")
        payload = {
            "id": entity_id,
            "session_id": event.session_id,
            "run_id": str(payload.get("turn_id") or "") or None,
            "name": str(payload.get("tool") or "Tool"),
            **({"title": str(payload["tool_title"])} if payload.get("tool_title") else {}),
            "state": "running",
            "input": payload.get("args"),
        }
    elif event_type == "tool.call.completed":
        event_type = "tool.upserted"
        entity_id = str(payload.get("call_id") or "")
        ok = bool(payload.get("ok"))
        payload = {
            "id": entity_id,
            "session_id": event.session_id,
            "name": str(payload.get("tool") or "Tool"),
            **({"title": str(payload["tool_title"])} if payload.get("tool_title") else {}),
            "state": "succeeded" if ok else "failed",
            "output": payload.get("result"),
            "duration_ms": payload.get("duration_ms"),
            **({"error": str(payload.get("error"))} if payload.get("error") else {}),
        }
    elif event_type == "permission.requested":
        event_type = "approval.upserted"
        tool_call = payload.get("tool_call")
        if not isinstance(tool_call, Mapping):
            tool_call = {}
        entity_id = str(payload.get("permission_id") or payload.get("id") or "")
        payload = {
            "id": entity_id,
            "session_id": str(payload.get("session_id") or event.session_id),
            "tool_name": str(
                tool_call.get("tool_name")
                or payload.get("tool_name")
                or payload.get("kind")
                or "Protected action"
            ),
            "input": tool_call.get("input", payload.get("input")),
            "summary": str(payload.get("summary") or "Protected action requires approval"),
            "status": "pending",
            "created_at": str(payload.get("created_at") or event.occurred_at),
            **({"reason": str(payload["reason"])} if payload.get("reason") else {}),
            **({"risk": str(payload["risk"])} if payload.get("risk") else {}),
        }
    elif event_type == "permission.resolved":
        event_type = "approval.resolved"
        entity_id = str(payload.get("permission_id") or payload.get("id") or "")
        action = str(payload.get("action") or "deny")
        payload = {
            "id": entity_id,
            "action": action,
            "status": "approved" if action.startswith("allow") else "denied",
            "resolved_at": event.occurred_at,
        }
    elif event_type.startswith("user_question."):
        event_type = "question.upserted"
        entity_id = str(payload.get("id") or payload.get("question_id") or "")
    elif event_type.startswith("agent.task."):
        event_type = "subagent.upserted"
        entity_id = str(payload.get("task_id") or payload.get("handle_id") or "")
        agent_ref = payload.get("agent_ref")
        if not isinstance(agent_ref, Mapping):
            agent_ref = {}
        run_index = int(payload.get("run_index") or 0)
        expert_id = str(agent_ref.get("expert_id") or "agent")
        title = str(payload.get("run_label") or f"{expert_id} #{run_index + 1}")
        raw_state = str(payload.get("live_state") or payload.get("status") or "interrupted")
        summary = payload.get("error_reason")
        result = payload.get("result")
        if not summary and isinstance(result, Mapping):
            summary = result.get("answer_excerpt")
        payload = {
            "id": entity_id,
            "session_id": str(payload.get("parent_session_id") or event.session_id),
            "parent_run_id": str(payload.get("parent_turn_id") or "") or None,
            "title": title,
            "state": _LIVE_WORK_STATE.get(raw_state, "interrupted"),
            "agent_id": expert_id,
            **(
                {"child_session_id": str(payload["child_session_id"])}
                if payload.get("child_session_id")
                else {}
            ),
            **({"summary": str(summary)} if summary else {}),
        }

    scope: dict[str, str] = {"connection_id": CONNECTION_ID}
    effective_workspace = ""
    if event.session_id:
        effective_workspace = workspace_id or str(getattr(session, "workspace_id", "") or "")
    if effective_workspace:
        scope["workspace_id"] = effective_workspace
    if event.session_id:
        scope["session_id"] = event.session_id
    run_id = payload.get("run_id") or payload.get("turn_id")
    if run_id:
        scope["run_id"] = str(run_id)

    envelope: dict[str, Any] = {
        "protocol_version": GACT_V3,
        "type": event_type,
        "occurred_at": event.occurred_at,
        "scope": scope,
        "entity_revision": event.id,
        "payload": payload,
    }
    if entity_id:
        envelope["entity_id"] = entity_id
    if event.replay:
        envelope["replay"] = True
    return envelope


def format_sse_v3(event: Event, *, session: Any = None, workspace_id: str = "") -> bytes:
    """Render an event as an SSE frame containing a GACT 0.3 envelope."""

    envelope = event_to_v3(event, session=session, workspace_id=workspace_id)
    event_type = str(envelope["type"])
    return (
        f"event: {event_type}\nid: {event.id}\ndata: "
        f"{json.dumps(envelope, separators=(',', ':'))}\n\n"
    ).encode()
