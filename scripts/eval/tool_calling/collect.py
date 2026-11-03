"""Normalize a ``GET /v1/sessions/{sid}/export`` body into the run shape metrics read.

The export (``clio.session-export.v2``) holds the root session's messages and
full semantic events (the durable trace, else ARC), plus ``children``: every
descendant with its own messages, events and ``relationship``
(``session_id``, ``parent_session_id``, ``depth``, ``attribution``).

Normalized session::

    {"session_id", "parent_session_id", "depth", "attribution",
     "lm_calls":   [{"id", "step_id", "started_at", "ended_at", "duration_ms",
                     "ttft_ms", "usage", "finish_reason", "status", "model"}],
     "tool_calls": [{"id", "step_id", "tool", "args", "started_at", "ended_at",
                     "duration_ms", "ok"}],
     "steps":      [{"step_id", "index", "tools", "is_finish"}],
     "events":     {"compactions", "goal_redrives", "turns_completed", "turns_failed"},
     "prompt_tokens": [total input of each model call, in order],
     "compactions": [{"compaction_id", "trigger", "replaced", "before", "after",
                      "checkpoint_cache_read", "checkpoint_input", "checkpoint"}],
     "injections": {source: count}, "spill_reads": n}

Times are epoch seconds. An ``lm.call`` payload carries ``timestamp`` (when
the call ended) and ``usage``; ``started_at``, ``duration_ms`` and ``ttft_ms``
are used when present (Phase 0 adds them), otherwise derived where possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: Message stop reasons that are not a final answer.
NOT_FINAL = frozenset({"error", "cancelled", "incomplete", "waiting_user", "max_tokens"})
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
#: Where a large tool result is spilled for later reads (``shell_spill_store``).
SPILL_MARKER = "/tool-output/"


def epoch(value: Any) -> float | None:
    """ISO-8601 or epoch seconds to epoch seconds; ``None`` when absent or unreadable."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _usage(raw: Any) -> dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}
    return {name: int(raw.get(name) or 0) for name in USAGE_FIELDS}


def lm_call_row(event: dict[str, Any]) -> dict[str, Any]:
    """One ``lm.call`` semantic event as a normalized call."""
    payload = event.get("payload") or {}
    ended = epoch(payload.get("timestamp")) or epoch(event.get("occurred_at"))
    started = epoch(payload.get("started_at"))
    duration = _number(payload.get("duration_ms"))
    if started is None and duration is not None and ended is not None:
        started = ended - duration / 1000.0
    if duration is None and started is not None and ended is not None:
        duration = (ended - started) * 1000.0
    return {
        "id": str(event.get("event_id") or ""),
        "step_id": str(event.get("parent_span_id") or ""),
        "started_at": started,
        "ended_at": ended,
        "duration_ms": duration,
        "ttft_ms": _number(payload.get("ttft_ms")),
        "usage": _usage(payload.get("usage")),
        "finish_reason": str(payload.get("finish_reason") or ""),
        "status": str(event.get("status") or ""),
        "model": str(payload.get("model") or ""),
        "error": str(payload.get("error") or ""),
    }


def _args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and set(raw) == {"kwargs"} and isinstance(raw["kwargs"], dict):
        return dict(raw["kwargs"])
    return dict(raw) if isinstance(raw, dict) else {}


def tool_call_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair ``tool.call.started`` with its completion by ``call_id``."""
    calls: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = str(event.get("event_type") or "")
        if kind not in ("tool.call.started", "tool.call.completed", "tool.call.failed"):
            continue
        payload = event.get("payload") or {}
        cid = str(payload.get("call_id") or (event.get("subject") or {}).get("call_id") or "")
        if not cid:
            continue
        row = calls.setdefault(
            cid,
            {
                "id": cid,
                "step_id": "",
                "tool": "",
                "args": {},
                "started_at": None,
                "ended_at": None,
                "duration_ms": None,
                "ok": None,
            },
        )
        row["tool"] = str(payload.get("tool") or row["tool"])
        if kind == "tool.call.started":
            row["step_id"] = str(event.get("parent_span_id") or "")
            row["args"] = _args(payload.get("args"))
            row["started_at"] = epoch(payload.get("started_at")) or epoch(event.get("occurred_at"))
        else:
            row["ended_at"] = epoch(payload.get("completed_at")) or epoch(event.get("occurred_at"))
            row["duration_ms"] = _number(payload.get("duration_ms"))
            row["ok"] = bool(payload.get("ok")) if "ok" in payload else kind.endswith("completed")
            row["step_id"] = row["step_id"] or str(event.get("parent_span_id") or "")
    for row in calls.values():
        if row["duration_ms"] is None and row["started_at"] and row["ended_at"]:
            row["duration_ms"] = (row["ended_at"] - row["started_at"]) * 1000.0
    return sorted(calls.values(), key=lambda r: (r["started_at"] or 0.0, r["id"]))


def step_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``react.step.completed`` events: the step span, its index and its tool names."""
    out = []
    for event in events:
        if event.get("event_type") != "react.step.completed":
            continue
        payload = event.get("payload") or {}
        out.append(
            {
                "step_id": str(payload.get("step_span_id") or ""),
                "index": payload.get("step_index"),
                "tools": [
                    str(c.get("name") or "")
                    for c in payload.get("tool_calls") or []
                    if isinstance(c, dict)
                ],
                "is_finish": bool(payload.get("is_finish")),
            }
        )
    return out


def _count(events: list[dict[str, Any]], *types: str) -> int:
    return sum(1 for e in events if e.get("event_type") in types)


def prompt_tokens(call: dict[str, Any]) -> int:
    """Everything a model call was sent: new, cached and cache-written input."""
    usage = call.get("usage") or {}
    return sum(
        int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    )


def compaction_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each completed compaction: the context before and after it, and the checkpoint call.

    The checkpoint call is the model call between ``compaction.started`` and
    ``compaction.completed``; before and after are the nearest model calls outside it.
    """
    rows = []
    started = {
        (e.get("payload") or {}).get("compaction_id"): epoch(e.get("occurred_at"))
        for e in events
        if e.get("event_type") == "compaction.started"
    }
    calls = [
        (epoch(e.get("occurred_at")) or 0.0, e) for e in events if e.get("event_type") == "lm.call"
    ]
    for event in events:
        if event.get("event_type") != "compaction.completed":
            continue
        payload = event.get("payload") or {}
        begin = started.get(payload.get("compaction_id")) or 0.0
        end = epoch(event.get("occurred_at")) or 0.0
        inside = [e for t, e in calls if begin <= t <= end]
        before = [e for t, e in calls if t < begin]
        after = [e for t, e in calls if t > end]
        checkpoint = inside[-1] if inside else None
        row = lm_call_row(checkpoint) if checkpoint else None
        rows.append(
            {
                "compaction_id": payload.get("compaction_id"),
                "trigger": payload.get("trigger"),
                "replaced": payload.get("replaced_count"),
                "before": prompt_tokens(lm_call_row(before[-1])) if before else None,
                "after": prompt_tokens(lm_call_row(after[0])) if after else None,
                "checkpoint_cache_read": (row or {}).get("usage", {}).get("cache_read_tokens"),
                "checkpoint_input": prompt_tokens(row) if row else None,
                "checkpoint": str(((checkpoint or {}).get("payload") or {}).get("content") or ""),
            }
        )
    return rows


def injection_counts(messages: list[dict[str, Any]]) -> dict[str, int]:
    """How many times each injection source added text to what the agent read."""
    counts: dict[str, int] = {}
    for message in messages:
        for part in message.get("parts") or []:
            if part.get("type") == "injection":
                source = str(part.get("source") or "")
                counts[source] = counts.get(source, 0) + 1
    return counts


def normalize_session(transcript: dict[str, Any]) -> dict[str, Any]:
    """One exported session (root or child) as a normalized session."""
    session = transcript.get("session") or {}
    relationship = transcript.get("relationship") or {}
    events = sorted(
        (e for e in transcript.get("semantic_events") or [] if isinstance(e, dict)),
        key=lambda e: epoch(e.get("occurred_at")) or 0.0,
    )
    lm_calls = sorted(
        (lm_call_row(e) for e in events if e.get("event_type") == "lm.call"),
        key=lambda c: (c["started_at"] or c["ended_at"] or 0.0, c["id"]),
    )
    return {
        "session_id": str(session.get("id") or relationship.get("session_id") or ""),
        "parent_session_id": str(
            relationship.get("parent_session_id") or session.get("parent_session_id") or ""
        ),
        "depth": int(relationship.get("depth") or 0),
        "attribution": str(relationship.get("attribution") or ("root" if not relationship else "")),
        "agent": str((session.get("agent") or {}).get("id") or ""),
        "lm_calls": lm_calls,
        "prompt_tokens": [prompt_tokens(c) for c in lm_calls],
        "compactions": compaction_rows(events),
        "injections": injection_counts(list(transcript.get("messages") or [])),
        "tool_calls": tool_call_rows(events),
        "steps": step_rows(events),
        "events": {
            "compactions": _count(events, "compaction.completed"),
            "compaction_failures": _count(events, "compaction.failed"),
            "goal_redrives": _count(events, "goal.redrive"),
            "turns_completed": _count(events, "turn.completed"),
            "turns_failed": _count(events, "turn.failed"),
        },
    }


def message_text(message: dict[str, Any]) -> str:
    """The visible text of a message."""
    return "\n".join(
        str(part.get("text") or "")
        for part in message.get("parts") or []
        if part.get("type") == "text" and part.get("text")
    ).strip()


def outcome(messages: list[dict[str, Any]], *, timed_out: bool = False) -> dict[str, Any]:
    """The completion guard: the run counts only if it ended with a final answer."""
    assistant = [m for m in messages if m.get("role") == "assistant"]
    last = max(assistant, key=lambda m: str(m.get("created_at") or "")) if assistant else None
    text = message_text(last) if last else ""
    stop = str((last or {}).get("stop_reason") or "")
    if timed_out:
        reason = "timed_out"
    elif last is None:
        reason = "no_reply"
    elif last.get("error_info"):
        reason = "error"
    elif stop in NOT_FINAL:
        reason = stop
    elif not text:
        reason = "empty_reply"
    else:
        reason = "final_answer"
    return {
        "final": reason == "final_answer",
        "reason": reason,
        "stop_reason": stop,
        "final_text": text,
        "message_id": str((last or {}).get("id") or ""),
    }


def normalize_export(export: dict[str, Any]) -> dict[str, Any]:
    """The whole export: sessions (root first), the outcome and recording notes."""
    sessions = [normalize_session(export)]
    sessions += [normalize_session(child) for child in export.get("children") or []]
    return {
        "sessions": sessions,
        "outcome": outcome(list(export.get("messages") or [])),
        "recording": export.get("recording") or {},
        "child_recordings": [
            {"session_id": (c.get("session") or {}).get("id"), **(c.get("recording") or {})}
            for c in export.get("children") or []
        ],
    }
