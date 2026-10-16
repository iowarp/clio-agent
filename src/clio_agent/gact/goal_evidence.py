"""Bounded recorded evidence for the independent goal judge."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.goal_steering import ordered_evidence


def bounded_inputs(value: Any) -> str:
    """Preserve relevant inputs while redacting structured credentials and large data."""
    secret_keys = {
        "api_key",
        "token",
        "password",
        "authorization",
        "secret",
        "access_token",
        "refresh_token",
    }

    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "[depth limited]"
        if isinstance(item, Mapping):
            return {
                str(k): "[redacted]" if str(k).lower() in secret_keys else clean(v, depth + 1)
                for k, v in list(item.items())[:30]
            }
        if isinstance(item, (list, tuple)):
            return [clean(v, depth + 1) for v in item[:20]]
        if isinstance(item, str):
            return item[:1000]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)[:1000]

    return json.dumps(clean(value), ensure_ascii=False)[:3000]


def _bounded_judge_fact(value: Any, *, limit: int = 500) -> str:
    """Return one compact fact for the judge without admitting raw result payloads."""

    return " ".join(str(value or "").split())[:limit]


def _tool_result_fact(part: Any) -> str:
    """Project the user-facing result summary already recorded on a tool part."""

    structured = getattr(part, "structured_content", None)
    raw = (getattr(part, "metadata", None) or {}).get("result")
    if not isinstance(structured, Mapping) and isinstance(raw, Mapping):
        structured = raw.get("structuredContent")
    if isinstance(structured, Mapping) and any(
        key in structured for key in ("exit_code", "stdout", "stderr", "timed_out")
    ):
        # Command text can be several thousand characters long. A prefix of
        # the whole result hides the actual exit status and test output from
        # the judge, causing already-completed phases to be repeated.
        facts = {key: structured[key] for key in ("exit_code", "timed_out") if key in structured}
        for key in ("stdout", "stderr"):
            text = str(structured.get(key) or "")
            if text:
                facts[key] = (
                    text
                    if len(text) <= 2400
                    else text[:800] + "\n[...truncated...]\n" + text[-1600:]
                )
        return json.dumps(facts, ensure_ascii=False)
    for owner, keys in (
        (getattr(part, "structured_content", None), ("message", "summary", "status")),
        (getattr(part, "presentation", None), ("summary",)),
        (getattr(part, "metadata", None), ("result",)),
    ):
        if not isinstance(owner, Mapping):
            continue
        for key in keys:
            fact = _bounded_judge_fact(owner.get(key))
            if fact:
                return fact
    for child in getattr(part, "content", []) or []:
        fact = _bounded_judge_fact(getattr(child, "text", ""))
        if fact:
            return fact
    return ""


def _build_transcript(app: Any, sid: str, *, max_messages: int = 40) -> str:
    """Render recent prose and bounded tool facts for the completion judge."""

    store = getattr(app.state, "messages", None)
    if store is None:
        return ""
    messages = store.get(sid, [])
    lines: list[str] = []
    recent = sorted(messages, key=lambda row: str(getattr(row, "created_at", "")))[-max_messages:]
    for message, parts in ordered_evidence(recent):
        role = str(getattr(message, "role", "") or "")
        turn_id = _bounded_judge_fact(getattr(message, "turn_id", ""), limit=120) or "unassigned"
        message_id = _bounded_judge_fact(getattr(message, "id", ""), limit=120) or "unassigned"
        lines.append(f"--- {role} turn {turn_id}; message {message_id} ---")
        for part in parts:
            part_type = str(getattr(part, "type", ""))
            if part_type == "text":
                text = str(getattr(part, "text", "") or "").strip()
                if text:
                    lines.append(f"{role}: {text}")
            elif part_type == "injection" and getattr(part, "source", "") in {
                "task_results",
                "goal_judgment",
                "loop_wake",
                "scheduled_wake",
                "task_completion",
            }:
                lines.append(
                    f"recorded harness injection ({getattr(part, 'source', '')}): "
                    + _bounded_judge_fact(getattr(part, "text", ""), limit=4000)
                )
            elif part_type == "tool_call":
                name = _bounded_judge_fact(getattr(part, "tool_name", ""), limit=120)
                if name:
                    inputs = getattr(part, "input", None)
                    suffix = f": {bounded_inputs(inputs)}" if inputs else ""
                    lines.append(f"{role} tool call: {name}{suffix}")
            elif part_type == "tool_result":
                name = _bounded_judge_fact(getattr(part, "tool_name", ""), limit=120)
                if not name:
                    continue
                outcome = "failed" if bool(getattr(part, "is_error", False)) else "completed"
                fact = _tool_result_fact(part)
                suffix = f": {fact}" if fact else ""
                lines.append(f"{role} tool result: {name} {outcome}{suffix}")
    return "\n".join(lines)[-32_000:]
