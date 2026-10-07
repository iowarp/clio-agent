"""Lossless session archives, independent of the bounded live UI projection."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.errors import ClioError
from clio_agent.gact.semantic_events import semantic_event_from_events_content
from clio_agent.gact.semantic_trace_file import FileSemanticTraceBackend
from clio_agent.gact.session_descendants import descendant_walk
from clio_agent.gact.transcript_file import file_transcript_enabled

if TYPE_CHECKING:
    from fastapi import FastAPI


def _events(app: FastAPI, sid: str) -> tuple[list[dict[str, Any]], str]:
    backend = getattr(app.state, "semantic_trace_backend", None)
    flush = getattr(backend, "flush", None)
    if callable(flush):
        flush()
    roots: list[Path | str] = list(getattr(backend, "replay_paths", ()))
    if isinstance(backend, FileSemanticTraceBackend):
        roots.append(backend.path)
    events: dict[str, dict[str, Any]] = {}
    for root in roots:
        root = Path(root)
        path = (
            root
            if root.suffix.lower() in FileSemanticTraceBackend._FILE_SUFFIXES
            else root / f"{sid}.semantic.jsonl"
        )
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)  # A damaged trace must not silently disappear.
                if event.get("session_id") == sid:
                    events[str(event["event_id"])] = event
    if events:
        return list(events.values()), "durable_trace"
    live = getattr(getattr(app.state, "arc", None), "_live", None)
    iterator: Callable[[str], Iterator[Any]] | None = getattr(
        live, "iter_session_event_segments", None
    )
    if callable(iterator):
        for segment in iterator(sid):
            event = semantic_event_from_events_content(
                segment.content, session_id=sid, turn_id=str(segment.turn_id or "")
            ).to_dict("full")
            event["event_id"] = event["span_id"] = str(segment.id)
            events[str(event["event_id"])] = event
    return list(events.values()), "arc" if events else "unavailable"


def _tool_records(
    messages: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = event.get("event_type")
        if kind not in {"tool.call.started", "tool.call.completed", "tool.call.failed"}:
            continue
        payload = event.get("payload") or {}
        cid = str(payload.get("call_id") or (event.get("subject") or {}).get("call_id") or "")
        if not cid:
            continue
        row = calls.setdefault(cid, {"call_id": cid, "source": "semantic_trace"})
        row["tool"] = payload.get("tool", row.get("tool", ""))
        row["status"] = event.get("status")
        row.setdefault("event_ids", []).append(event["event_id"])
        if kind == "tool.call.started":
            row["input"] = payload.get("args", {})
        elif "result" in payload:
            row["output"] = payload["result"]
        if payload.get("error"):
            row["error"] = payload["error"]
    # Older sessions may only have their bounded message rows. Preserve them,
    # explicitly labelled, rather than inventing the missing original bytes.
    for message in messages:
        for part in message.get("parts", []):
            if part.get("type") not in {"tool_call", "tool_result"}:
                continue
            cid = str(part.get("call_id") or part.get("id") or "")
            row = calls.setdefault(cid, {"call_id": cid, "source": "message_history"})
            key = "input" if part["type"] == "tool_call" else "output"
            if key in row:
                continue
            if row["source"] == "semantic_trace":
                row["source"] = "mixed_history"
            row["tool"] = part.get("name", row.get("tool", ""))
            row[key] = part.get("input") if key == "input" else part.get("result", part.get("text"))
    return list(calls.values())


def recorded_skills(
    events: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Match legacy loads by requested identity, including overlapping tool calls.

    Historical bodies are complete recorded tool responses, which may include
    several bundled references. An ambiguous association remains a gap rather
    than assigning whichever parallel call happened to start last.
    """
    active: dict[str, dict[str, Any]] = {}
    by_call = {row["call_id"]: row for row in tools}
    skills: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("event_type")
        payload = event.get("payload") or {}
        cid = str(payload.get("call_id") or (event.get("subject") or {}).get("call_id") or "")
        if kind == "tool.call.started" and payload.get("tool") == "load_skill":
            args = by_call.get(cid, {}).get("input") or payload.get("args") or {}
            if isinstance(args, dict):
                active[cid] = args.get("kwargs", args)
        elif kind == "skill.loaded":
            skill = {"event_id": event["event_id"], **payload}
            candidates = []
            for call_id, args in active.items():
                if args.get("skill_id") != skill.get("skill_id"):
                    continue
                files = [str(args.get("file") or ""), *(args.get("files") or [])]
                if str(skill.get("file") or "") in files:
                    candidates.append(call_id)
            if "content" not in skill and len(candidates) == 1:
                call_id = candidates[0]
                response = by_call.get(call_id, {}).get("output")
                if isinstance(response, str):
                    skill.update(
                        content=response,
                        content_source="recorded_tool_response",
                        content_scope="complete_tool_response",
                        call_id=call_id,
                    )
            skills.append(skill)
        elif kind in {"tool.call.completed", "tool.call.failed"}:
            active.pop(cid, None)
    return skills


def _session_transcript(app: FastAPI, sid: str) -> dict[str, Any]:
    from clio_agent.gact.routes.interactions import recorded_session_interactions

    session = app.state.sessions.get(sid)
    if session is None:
        raise KeyError(sid)
    workspace = app.state.workspaces.get(session.workspace_id)
    message_issues: list[dict[str, Any]] = []
    try:
        message_rows = app.state.messages.get(sid, [])
    except ClioError as exc:
        # An archive must remain obtainable from the recorded file ledger
        # when a cold ARC projection is damaged. Keep the diagnostic explicit
        # and never substitute an empty transcript for an unavailable store.
        store = getattr(app.state, "message_store", None)
        if not file_transcript_enabled(app) or store is None:
            raise ValueError(f"recorded transcript unavailable: {exc}") from exc
        message_rows = store.load_session(sid)
        if message_rows is None:
            raise ValueError(f"recorded transcript unavailable: {exc}") from exc
        message_issues.append(
            {
                "reason": "message_projection_unavailable",
                "message_source": "durable_file",
                "detail": str(exc),
            }
        )
    messages = [m.model_dump(mode="json", exclude_none=True) for m in message_rows]
    events, source = _events(app, sid)
    tools = _tool_records(messages, events)
    skills = recorded_skills(events, tools)
    issues = message_issues + [
        {"reason": "historical_tool_record_only", "call_id": row["call_id"]}
        for row in tools
        if row["source"] != "semantic_trace"
    ]
    issues.extend(
        {"reason": "skill_content_not_recorded", "event_id": row["event_id"]}
        for row in skills
        if "content" not in row
    )
    if source == "unavailable" and messages:
        issues.append({"reason": "semantic_trace_unavailable"})
    return {
        "session": session.to_wire(),
        "workspace": workspace.to_wire() if workspace else None,
        "messages": messages,
        "context_files": [dict(row) for row in app.state.context_files.get(sid, {}).values()],
        "semantic_events": events,
        "tool_records": tools,
        "loaded_skills": skills,
        "interactions": recorded_session_interactions(app, sid),
        "recording": {"source": source, "issues": issues},
    }


def build_transcript(app: FastAPI, sid: str) -> dict[str, Any]:
    """Export recorded messages, full events, tools and loaded skills, including children.

    The legacy top-level JSON shape remains importable. The archive extension is
    versioned separately; no event or tool payload is capped for UI rendering.
    """
    root = _session_transcript(app, sid)
    walk, truncated = descendant_walk(app, sid)
    children: list[dict[str, Any]] = []
    for item in walk:
        if app.state.sessions.get(item.session_id) is None:
            root["recording"]["issues"].append(
                {"reason": "child_session_unavailable", "session_id": item.session_id}
            )
            continue
        children.append({**_session_transcript(app, item.session_id), "relationship": asdict(item)})
    if truncated:
        root["recording"]["issues"].append({"reason": "descendant_depth_limit"})
    return {
        "version": "1",
        "export_schema": "clio.session-export.v2",
        "mode": "transcript",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        **root,
        "children": children,
    }
