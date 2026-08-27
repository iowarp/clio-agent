"""Event projections and SSE framing for GACT 0.3."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3 import CONNECTION_ID, GACT_V3
from clio_agent.gact.protocol.v3.message import message_to_v3, part_to_v3_block
from clio_agent.gact.protocol.v3.session import session_to_v3

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


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a typed mapping view for untrusted event payload values."""

    return value if isinstance(value, Mapping) else {}


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


@dataclass(frozen=True)
class _Projection:
    event_type: str
    payload: dict[str, Any]
    entity_id: str | None = None


_Projector = Callable[[Event, dict[str, Any], Any], _Projection | None]


def _stream_live(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del event, session
    return _Projection("stream.live", payload)


def _session_upsert(event: Event, payload: dict[str, Any], session: Any) -> _Projection | None:
    del event, payload
    if session is None:
        return None
    projected = session_to_v3(session)
    return _Projection("session.upserted", projected, str(projected["id"]))


def _message_upsert(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del event, session
    projected = message_to_v3(payload)
    return _Projection("message.upserted", projected, str(projected["id"]))


def _message_block_upsert(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del event, session
    block = part_to_v3_block(_mapping(payload.get("part")))
    projected = {"message_id": str(payload.get("message_id") or ""), "block": block}
    return _Projection("message.block.upserted", projected, str(block.get("id") or ""))


def _message_block_delta(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del event, session
    raw_delta = payload.get("delta")
    delta = raw_delta.get("text_append") if isinstance(raw_delta, Mapping) else raw_delta
    block_id = str(payload.get("part_id") or "")
    projected = {
        "message_id": str(payload.get("message_id") or ""),
        "block_id": block_id,
        "delta": str(delta or ""),
    }
    return _Projection("message.block.delta", projected, block_id)


def _message_block_completed(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del event, session
    block_id = str(payload.get("part_id") or "")
    projected = {
        "message_id": str(payload.get("message_id") or ""),
        "block_id": block_id,
        "text": str(payload.get("final_text") or ""),
    }
    return _Projection("message.block.completed", projected, block_id)


def _message_completed(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del session
    tokens = _mapping(payload.get("tokens"))
    message_id = str(payload.get("message_id") or "")
    projected = {
        "message_id": message_id,
        "completed_at": event.occurred_at,
        "stop_reason": str(payload.get("stop_reason") or "end_turn"),
        "tokens": {
            "input": int(tokens.get("input") or 0),
            "output": int(tokens.get("output") or 0),
            "cache_read": int(tokens.get("cache_read") or 0),
            "cache_write": int(tokens.get("cache_write") or 0),
        },
        "cost_usd": payload.get("cost_usd"),
        **({"error_info": payload["error_info"]} if payload.get("error_info") else {}),
    }
    return _Projection("message.completed", projected, message_id)


def _tool_started(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del session
    entity_id = str(payload.get("call_id") or "")
    projected = {
        "id": entity_id,
        "session_id": event.session_id,
        "run_id": str(payload.get("turn_id") or "") or None,
        "name": str(payload.get("tool") or "Tool"),
        **({"title": str(payload["tool_title"])} if payload.get("tool_title") else {}),
        "state": "running",
        "input": payload.get("args"),
    }
    return _Projection("tool.upserted", projected, entity_id)


def _tool_completed(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del session
    entity_id = str(payload.get("call_id") or "")
    ok = bool(payload.get("ok"))
    projected = {
        "id": entity_id,
        "session_id": event.session_id,
        "name": str(payload.get("tool") or "Tool"),
        **({"title": str(payload["tool_title"])} if payload.get("tool_title") else {}),
        "state": "succeeded" if ok else "failed",
        "output": payload.get("result"),
        "duration_ms": payload.get("duration_ms"),
        **({"error": str(payload["error"])} if payload.get("error") else {}),
    }
    return _Projection("tool.upserted", projected, entity_id)


def _permission_requested(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del session
    tool_call = _mapping(payload.get("tool_call"))
    entity_id = str(payload.get("permission_id") or payload.get("id") or "")
    projected = {
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
    return _Projection("approval.upserted", projected, entity_id)


def _permission_resolved(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del session
    entity_id = str(payload.get("permission_id") or payload.get("id") or "")
    action = str(payload.get("action") or "deny")
    projected = {
        "id": entity_id,
        "action": action,
        "status": "approved" if action.startswith("allow") else "denied",
        "resolved_at": event.occurred_at,
    }
    return _Projection("approval.resolved", projected, entity_id)


def _question_upsert(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del event, session
    entity_id = str(payload.get("id") or payload.get("question_id") or "")
    return _Projection("question.upserted", payload, entity_id)


def _subagent_upsert(event: Event, payload: dict[str, Any], session: Any) -> _Projection:
    del session
    entity_id = str(payload.get("task_id") or payload.get("handle_id") or "")
    agent_ref = _mapping(payload.get("agent_ref"))
    run_index = int(payload.get("run_index") or 0)
    expert_id = str(agent_ref.get("expert_id") or "agent")
    title = str(payload.get("run_label") or f"{expert_id} #{run_index + 1}")
    raw_state = str(payload.get("live_state") or payload.get("status") or "interrupted")
    summary = payload.get("error_reason")
    result = payload.get("result")
    answer_excerpt = ""
    if not summary and isinstance(result, Mapping):
        answer_excerpt = str(result.get("answer_excerpt") or "")
        summary = answer_excerpt
    projected = {
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
        **({"result": answer_excerpt} if answer_excerpt else {}),
    }
    return _Projection("subagent.upserted", projected, entity_id)


_EVENT_PROJECTORS: dict[str, _Projector] = {
    "server.connected": _stream_live,
    "session.snapshot": _session_upsert,
    "session.status_changed": _session_upsert,
    "session.updated": _session_upsert,
    "message.created": _message_upsert,
    "message.part.added": _message_block_upsert,
    "message.part.updated": _message_block_upsert,
    "message.part.delta": _message_block_delta,
    "message.part.completed": _message_block_completed,
    "message.completed": _message_completed,
    "tool.call.started": _tool_started,
    "tool.call.completed": _tool_completed,
    "permission.requested": _permission_requested,
    "permission.resolved": _permission_resolved,
}

_PREFIX_PROJECTORS: tuple[tuple[str, _Projector], ...] = (
    ("user_question.", _question_upsert),
    ("agent.task.", _subagent_upsert),
)


def _projector_for(event_type: str) -> _Projector | None:
    projector = _EVENT_PROJECTORS.get(event_type)
    if projector is not None:
        return projector
    return next(
        (candidate for prefix, candidate in _PREFIX_PROJECTORS if event_type.startswith(prefix)),
        None,
    )


def event_to_v3(event: Event, *, session: Any = None, workspace_id: str = "") -> dict[str, Any]:
    """Translate a live 0.2 event into the canonical scoped 0.3 envelope."""

    payload: dict[str, Any] = dict(event.payload)
    event_type = event.type
    entity_id = _event_identity(event_type, payload)

    projector = _projector_for(event_type)
    if projector is not None:
        projected = projector(event, payload, session)
        if projected is not None:
            event_type = projected.event_type
            payload = projected.payload
            entity_id = projected.entity_id

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
