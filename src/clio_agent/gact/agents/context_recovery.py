"""Recover absent context from recorded operations, with an inspectable receipt."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

import msgspec

from clio_agent.arc.schema import WORKING_SET_KINDS, Segment
from clio_agent.gact import context


def context_identity(arc: Any, session: str, scope: str) -> dict[str, Any]:
    """Describe the actual context plane without credentials or model internals."""
    store = getattr(arc, "_store", None)
    return {
        "process_id": os.getpid(),
        "mode": "clio_core" if store is not None else "history",
        "session_id": session,
        "scope": scope,
        "has_segments": bool(arc and arc.has_segments(session, scope)),
        "store_config": str(getattr(store, "_config_path", "")),
        "store_namespace": str(getattr(store, "_namespace", "")),
    }


def recorded_context(session: str, scope: str, ledger: Sequence[Any]) -> list[Segment]:
    """Replay this scope's operations in durable append order, respecting rollback.

    Clocks can reset when a store changes; sorting by a process-local clock would
    reorder edits. Keep segment identities and tombstones, and reject malformed
    records rather than quietly dropping a command or result.
    """
    app = context.active_app()
    backend = getattr(getattr(app, "state", None), "semantic_trace_backend", None)
    reader = getattr(backend, "session_events", None)
    if not callable(reader):
        return []
    kept = {str(field(row, "id") or "") for row in ledger if field(row, "role") == "user"}
    state: dict[str, Segment] = {}
    for event in cast(Iterable[dict[str, Any]], reader(session)):
        if event.get("event_type") != "arc.op":
            continue
        payload = event.get("payload") or {}
        if payload.get("scope") != scope:
            continue
        turn = str(event.get("turn_id") or "")
        if turn and turn not in kept:
            continue
        if payload.get("op") == "restore":
            # A restored snapshot replaces a lost plane, rather than appending
            # a second copy of its pre-loss history with newly reconstructed IDs.
            state.clear()
        for raw in payload.get("segments_written") or []:
            seg = msgspec.convert(raw, Segment)
            if seg.session_id != session or seg.scope != scope:
                raise ValueError("A context operation contains a foreign segment")
            state[seg.id] = seg
        for identity in payload.get("segments_tombstoned") or []:
            if str(identity) in state:
                state[str(identity)].status = "tombstoned"
                state[str(identity)].tombstoned_at = int(payload.get("logical_time") or 0)
    # Retain tombstones too: undo and provenance still refer to their identities.
    return sorted(state.values(), key=lambda segment: (segment.order, segment.logical_time))


def recovery_details(note: str, segments: Sequence[Segment]) -> str:
    """Format the exact restored records for the user's transcript and popup."""
    rows = [note, "", "Recorded context restored in this order:"]
    for segment in segments:
        if segment.status != "live" or segment.kind not in WORKING_SET_KINDS:
            continue
        rows.extend(["", f"--- {segment.kind} · turn {segment.turn_id or 'unassigned'} ---"])
        # JSON preserves nested inputs, errors, media descriptors and continuation
        # fields without an invented paraphrase or an omitted result.
        rows.append(json.dumps(segment.content, ensure_ascii=False, indent=2))
    return "\n".join(rows)


def field(row: Any, name: str) -> Any:
    """Read a live model or a stored JSON row consistently."""
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def observation_value(part: Any) -> Any:
    """Recover saved result text without Python repr or whitespace normalization."""
    content = field(part, "content")
    if isinstance(content, (list, tuple)):
        texts = [field(item, "text") for item in content]
        if all(isinstance(text, str) for text in texts):
            return "".join(texts)
        return json.dumps(msgspec.to_builtins(content), ensure_ascii=False)
    return content if content is not None else field(part, "text")


def transcript_records(row: Any) -> list[tuple[str, dict[str, Any]]]:
    """Recover public transcript parts when no plane operations exist.

    This is explicitly a transcript reconstruction, not provider continuation.
    Historical tool records are preserved when present. Missing inputs/results
    are stated as gaps, never replaced by fabricated successful calls.
    """
    from clio_agent.gact.summarization_record import row_summarization

    summary = row_summarization(row)
    if summary is not None and summary.text:
        return [("summary", {"text": summary.text})]
    role = field(row, "role")
    records: list[tuple[str, dict[str, Any]]] = []
    metadata = field(row, "metadata") or {}
    telemetry = iter(metadata.get("tools_called") or [])
    open_calls: dict[str, str] = {}
    for part in field(row, "parts") or []:
        kind, text = field(part, "type"), field(part, "text")
        if kind == "text" and text:
            records.append(("user" if role == "user" else "thought", {"text": text}))
        elif kind == "thinking" and text:
            records.append(
                ("thought", {"text": "", "thinking": [{"text": text, "continuation": []}]})
            )
        elif kind == "injection" and text:
            records.append(
                ("user", {"text": text, "actor": "algorithm", "source": field(part, "source")})
            )
        elif kind == "tool_call":
            detail: Mapping[str, Any] = next(telemetry, {})
            args = field(part, "input")
            if args is None:
                args = detail.get("args")
            call_id = str(field(part, "call_id") or field(part, "id"))
            name = str(field(part, "tool_name") or detail.get("name") or "")
            if not isinstance(args, Mapping) or not name:
                records.append(
                    (
                        "user",
                        {
                            "actor": "algorithm",
                            "source": "recovery_gap",
                            "text": f"Recorded input unavailable for {name or call_id}.",
                        },
                    )
                )
                continue
            records.append(("tool_call", {"id": call_id, "name": name, "args": dict(args)}))
            open_calls[call_id] = name
        elif kind == "tool_result":
            call_id = str(field(part, "call_id") or "")
            if call_id in open_calls:
                value = observation_value(part)
                records.append(
                    (
                        "observation",
                        {
                            "call_id": call_id,
                            "text": value if value is not None else "[Recorded result unavailable]",
                            "is_error": bool(field(part, "is_error")) or value is None,
                        },
                    )
                )
                del open_calls[call_id]
    if open_calls:
        raise ValueError(
            "Recorded conversation has unanswered tool calls; recovery needs their results"
        )
    return records


def transcript_context(session: str, scope: str, rows: Sequence[Any]) -> list[Segment]:
    """Build the whole fallback before writing, retaining the original turn IDs."""
    segments: list[Segment] = []
    turn = ""
    for row in rows:
        if field(row, "role") == "user":
            turn = str(field(row, "id") or "")
        for kind, content in transcript_records(row):
            number = len(segments) + 1
            segments.append(
                msgspec.convert(
                    {
                        "session_id": session,
                        "scope": scope,
                        "kind": kind,
                        "step": 0,
                        "content": content,
                        "turn_id": turn,
                        "logical_time": number,
                        "order": float(number),
                    },
                    Segment,
                )
            )
    return segments
