"""Synthetic session exports and run results in the shapes the server produces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

T0 = 1_790_000_000.0


def iso(offset: float) -> str:
    return datetime.fromtimestamp(T0 + offset, UTC).isoformat()


def lm_event(step: str, *, end: float, usage: dict[str, int], **timing: float) -> dict[str, Any]:
    payload: dict[str, Any] = {"timestamp": iso(end), "usage": usage, "model": "claude-sonnet-5-5"}
    if "start" in timing:
        payload["started_at"] = iso(timing["start"])
        payload["duration_ms"] = (end - timing["start"]) * 1000.0
    if "ttft" in timing:
        payload["ttft_ms"] = timing["ttft"]
    return {
        "event_id": f"lm-{step}-{end}",
        "event_type": "lm.call",
        "parent_span_id": step,
        "status": "completed",
        "occurred_at": iso(end),
        "payload": payload,
    }


def tool_events(
    call: str, tool: str, step: str, start: float, end: float, args: Any = None
) -> list[dict[str, Any]]:
    return [
        {
            "event_id": f"{call}-s",
            "event_type": "tool.call.started",
            "parent_span_id": step,
            "status": "running",
            "occurred_at": iso(start),
            "subject": {"call_id": call},
            "payload": {
                "call_id": call,
                "tool": tool,
                "started_at": iso(start),
                "args": args or {},
            },
        },
        {
            "event_id": f"{call}-c",
            "event_type": "tool.call.completed",
            "parent_span_id": step,
            "status": "completed",
            "occurred_at": iso(end),
            "subject": {"call_id": call},
            "payload": {
                "call_id": call,
                "tool": tool,
                "completed_at": iso(end),
                "ok": True,
                "duration_ms": (end - start) * 1000.0,
            },
        },
    ]


def step_event(step: str, index: int, tools: list[str]) -> dict[str, Any]:
    return {
        "event_id": f"step-{step}",
        "event_type": "react.step.completed",
        "status": "completed",
        "occurred_at": iso(index),
        "payload": {
            "step_span_id": step,
            "step_index": index,
            "tool_calls": [{"name": t} for t in tools],
            "is_finish": not tools,
        },
    }


def export(final_text: str = "The answer is 42.", stop_reason: str = "end_turn") -> dict[str, Any]:
    """Root: three steps (a parallel pair, one search_tools call, a final step); one child."""
    root_events = [
        lm_event("s1", start=0.0, end=2.0, ttft=400.0, usage={"input_tokens": 1000, "cache_read_tokens": 20000, "output_tokens": 100}),
        *tool_events("call_a", "fs_read_file", "s1", 2.1, 2.6, {"kwargs": {"path": "a.py"}}),
        *tool_events("call_b", "fs_read_file", "s1", 2.1, 3.1, {"path": "b.py"}),
        step_event("s1", 1, ["fs_read_file", "fs_read_file"]),
        lm_event("s2", start=3.5, end=5.0, ttft=300.0, usage={"input_tokens": 200, "cache_read_tokens": 21000, "output_tokens": 50}),
        *tool_events("call_c", "search_tools", "s2", 5.1, 5.3),
        lm_event("s3", start=6.0, end=7.0, usage={"input_tokens": 300, "cache_read_tokens": 22000, "output_tokens": 30}),
        {"event_id": "cmp", "event_type": "compaction.completed", "occurred_at": iso(5.5), "payload": {}},
    ]  # fmt: skip
    child_events = [
        lm_event("c1", end=4.0, usage={"input_tokens": 50, "output_tokens": 5}),
        lm_event("c2", end=4.5, usage={"input_tokens": 60, "output_tokens": 6}),
        *tool_events("call_d", "write_todos", "c1", 4.1, 4.2),
    ]
    return {
        "export_schema": "clio.session-export.v2",
        "session": {"id": "sess_root", "agent": {"id": "standard"}},
        "messages": [
            {
                "id": "m1",
                "role": "user",
                "created_at": iso(0),
                "parts": [{"type": "text", "text": "q"}],
            },
            {
                "id": "m2",
                "role": "assistant",
                "created_at": iso(7.0),
                "updated_at": iso(7.2),
                "stop_reason": stop_reason,
                "parts": [{"type": "text", "text": final_text}],
            },
        ],
        "semantic_events": root_events,
        "recording": {"source": "durable_trace", "issues": []},
        "children": [
            {
                "session": {"id": "sess_child", "parent_session_id": "sess_root"},
                "relationship": {
                    "session_id": "sess_child",
                    "parent_session_id": "sess_root",
                    "depth": 1,
                    "attribution": "agent_task",
                },
                "messages": [],
                "semantic_events": child_events,
                "recording": {"source": "durable_trace", "issues": []},
            }
        ],
    }


def fleet(
    weekly: float, five: float = 10.0, resets: str = "2026-11-08T00:00:00+00:00"
) -> dict[str, Any]:
    return {
        "accounts": [
            {
                "id": "claude_code",
                "usage": {
                    "windows": [
                        {"name": "5-hour", "used_percent": five, "resets_at": "x", "window_minutes": 300},
                        {"name": "Weekly", "used_percent": weekly, "resets_at": resets, "window_minutes": 10080},
                    ]
                },
            }
        ]
    }  # fmt: skip
