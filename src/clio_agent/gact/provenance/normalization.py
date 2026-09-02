"""Provider-neutral execution-provenance read model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

EXECUTION_PROVENANCE_SCHEMA = "clio.execution_provenance.v1"
_TERMINAL_SUFFIXES = (".completed", ".failed", ".error", ".cancelled")
_START_SUFFIXES = (".started", ".running")
_SPAN_FAMILIES = {
    "expert.lifecycle.started": "expert.lifecycle",
    "expert.extract.completed": "expert.lifecycle",
    "llm.request.started": "llm.request",
    "llm.response.completed": "llm.request",
}


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _base_event_type(event_type: str) -> str:
    family = _SPAN_FAMILIES.get(event_type)
    if family is not None:
        return family
    for suffix in (*_START_SUFFIXES, *_TERMINAL_SUFFIXES):
        if event_type.endswith(suffix):
            return event_type[: -len(suffix)]
    return event_type


def _event_kind(event_type: str) -> str:
    if event_type.startswith("session."):
        return "session"
    if event_type.startswith("turn."):
        return "turn"
    if event_type.startswith(("expert.", "agent.", "delegation.", "blueprint.delegation.")):
        return "agent"
    if event_type.startswith(("lm.", "provider.")):
        return "llm"
    if event_type.startswith(("tool.", "react.step")):
        return "tool"
    if event_type.startswith("artifact."):
        return "artifact"
    if event_type.startswith(("permission.", "question.", "interaction.")):
        return "interaction"
    if event_type.startswith(("a2ui.", "mcp.task.", "mcp_task.")):
        return "interactive_work"
    if event_type.startswith(("resource.", "evidence.", "context.reference.")):
        return "resource"
    return "event"


def _correlation(event: dict[str, Any]) -> str:
    if event.get("correlation_id"):
        return str(event["correlation_id"])
    payload = event.get("payload") or {}
    if isinstance(payload, dict):
        for key in (
            "tool_call_id",
            "expert_span_id",
            "step_span_id",
            "invocation_id",
            "attempt_id",
            "task_id",
        ):
            if payload.get(key):
                return str(payload[key])
    for key in ("turn_id", "span_id", "event_id"):
        if event.get(key):
            return str(event[key])
    return ""


def normalize_semantic_events(
    events: list[dict[str, Any]],
    *,
    provider: str,
    session_id: str,
    provider_health: dict[str, Any] | None = None,
    limit: int = 2000,
) -> dict[str, Any]:
    """Fold semantic events into provider-neutral spans, nodes, and edges."""
    ordered = sorted(events, key=lambda event: _timestamp(event.get("occurred_at")) or 0.0)
    spans: list[dict[str, Any]] = []
    open_spans: dict[tuple[str, str, str], int] = {}
    seen_ids: set[str] = set()

    for position, event in enumerate(ordered[-limit:]):
        event_type = str(event.get("event_type") or "event")
        base_type = _base_event_type(event_type)
        correlation = _correlation(event)
        event_session = str(event.get("session_id") or session_id)
        key = (event_session, base_type, correlation)
        status = str(event.get("status") or "completed").lower()
        occurred = _timestamp(event.get("occurred_at"))
        # A running status does not necessarily open a lifecycle span. Token deltas,
        # progress samples, and other one-shot observations use it too. Lifecycle
        # opening is carried by the event type; terminal status remains useful for
        # one-shot completed/failed records.
        is_start = event_type.endswith(_START_SUFFIXES)
        is_terminal = event_type.endswith(_TERMINAL_SUFFIXES) or status in {
            "completed",
            "finished",
            "success",
            "failed",
            "error",
            "cancelled",
            "unknown",
        }
        raw_actor = event.get("actor")
        raw_payload = event.get("payload")
        actor: dict[str, Any] = raw_actor if isinstance(raw_actor, dict) else {}
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        raw_subject = event.get("subject")
        subject: dict[str, Any] = raw_subject if isinstance(raw_subject, dict) else {}
        identity = {
            key: str(payload.get(key) or subject.get(key) or "")
            for key in (
                "task_id",
                "invocation_id",
                "tool_name",
                "surface_id",
                "interaction_id",
                "question_id",
                "permission_id",
                "resource_id",
            )
        }
        span_id = str(event.get("span_id") or event.get("event_id") or f"event-{position}")
        if span_id in seen_ids:
            span_id = f"{span_id}-{position}"
        seen_ids.add(span_id)

        if is_terminal and key in open_spans:
            span = spans[open_spans.pop(key)]
            span["status"] = status
            span["end_time"] = occurred
            if span.get("start_time") is not None and occurred is not None:
                span["duration_ms"] = max(0.0, (occurred - float(span["start_time"])) * 1000.0)
            span["source_event_ids"].append(str(event.get("event_id") or span_id))
            span["attributes"]["terminal_event_type"] = event_type
            continue

        if is_start and key in open_spans:
            # Streamed and synchronous instrumentation can both announce the same
            # logical LM request. With no distinct correlation id they are duplicate
            # evidence for one span, not two independently closable lifecycles.
            span = spans[open_spans[key]]
            span["source_event_ids"].append(str(event.get("event_id") or span_id))
            duplicate_types = span["attributes"].setdefault("start_event_types", [])
            if event_type not in duplicate_types:
                duplicate_types.append(event_type)
            continue

        span = {
            "id": span_id,
            "parent_id": str(event.get("parent_span_id") or ""),
            "kind": _event_kind(event_type),
            "session_id": event_session,
            "workflow_id": str(payload.get("workflow_id") or ""),
            "campaign_id": str(payload.get("campaign_id") or ""),
            "agent_id": str(actor.get("agent_id") or payload.get("expert_id") or ""),
            "source_agent_id": str(payload.get("source_agent_id") or ""),
            "task_id": identity["task_id"],
            "invocation_id": identity["invocation_id"],
            "tool_name": identity["tool_name"],
            "surface_id": identity["surface_id"],
            "label": str(event.get("summary") or event_type),
            "event_type": event_type,
            "status": status,
            "start_time": occurred,
            "end_time": occurred if is_terminal and not is_start else None,
            "duration_ms": 0.0 if is_terminal and not is_start else None,
            "host": str(payload.get("hostname") or ""),
            "artifact_refs": _artifact_refs(event),
            "attributes": {
                "trace_id": str(event.get("trace_id") or ""),
                "turn_id": str(event.get("turn_id") or ""),
                "workspace_id": str(event.get("workspace_id") or ""),
                "schema_version": str(event.get("schema_version") or ""),
                "provider": event.get("provider") or {},
                **{key: value for key, value in identity.items() if value},
            },
            "source_event_ids": [str(event.get("event_id") or span_id)],
        }
        spans.append(span)
        if is_start and not is_terminal:
            open_spans[key] = len(spans) - 1

    nodes = [
        {
            "id": span["id"],
            "kind": span["kind"],
            "label": span["label"],
            "status": span["status"],
            "session_id": span["session_id"],
            "agent_id": span["agent_id"],
            "start_time": span["start_time"],
            "end_time": span["end_time"],
            "attributes": span["attributes"],
        }
        for span in spans
    ]
    node_ids = {node["id"] for node in nodes}
    edges = [
        {
            "id": f"{span['parent_id']}->{span['id']}",
            "source": span["parent_id"],
            "target": span["id"],
            "kind": "contains",
        }
        for span in spans
        if span["parent_id"] and span["parent_id"] in node_ids
    ]
    return {
        "schema_version": EXECUTION_PROVENANCE_SCHEMA,
        "provider": provider,
        "session_id": session_id,
        "complete": not bool(open_spans),
        "truncated": len(events) > limit,
        "provider_health": provider_health or {},
        "campaigns": _distinct_entities(spans, "campaign_id"),
        "workflows": _distinct_entities(spans, "workflow_id"),
        "agents": _distinct_entities(spans, "agent_id"),
        "spans": spans,
        "nodes": nodes,
        "edges": edges,
    }


def _artifact_refs(event: dict[str, Any]) -> list[dict[str, str]]:
    raw_payload = event.get("payload")
    raw_subject = event.get("subject")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    subject: dict[str, Any] = raw_subject if isinstance(raw_subject, dict) else {}
    refs: list[dict[str, str]] = []
    for source in (subject, payload):
        artifact_id = source.get("artifact_id") or source.get("version_id")
        if artifact_id:
            refs.append(
                {
                    "artifact_id": str(artifact_id),
                    "sha256": str(source.get("sha256") or source.get("hash") or ""),
                }
            )
    return refs


def _distinct_entities(spans: list[dict[str, Any]], field: str) -> list[dict[str, str]]:
    values = sorted({str(span.get(field) or "") for span in spans if span.get(field)})
    return [{"id": value} for value in values]
