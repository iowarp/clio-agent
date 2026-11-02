"""Recorded evidence for the independent goal judge, whole up to the judge's window.

The judge decides whether a goal is met, and so whether the agent is driven again. It
reads what the session recorded: prose, tool calls with their inputs, tool results,
harness injections, the plan and the todos. CLIO does not choose how much of each it may
read: cutting results, inputs and the transcript made the judge miss finished work (a
command's exit status, a saved file) and send the agent back to repeat completed phases.

The one limit is real: the judge model's context window (:func:`judge_room`). The room
is the window up to the auto-compaction threshold (the headroom the agent loop keeps for
framing and the answer), never more than the window less the judge's declared output
tokens, less the judge's other inputs (instructions, goal condition, retained progress
ledger). The plan and todos are the current state and are kept first; transcript entries
are then kept whole, newest first. The first entry that does not fit is left out with
everything older, and the transcript opens with a note that says so and points the judge
to its retained progress ledger. The fit is traced as
:data:`EVIDENCE_EXCEEDS_JUDGE_CONTEXT`. When even the newest entry does not fit, its most
recent part is kept and marked. When the judge's window is unknown there is no limit to
gate on: the evidence is given whole, traced as :data:`JUDGE_CONTEXT_WINDOW_UNKNOWN`.

Credential redaction is a safety property, not a size bound: values under keys that name
a credential are replaced with ``[redacted]`` in every recorded tool input.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from clio_agent.gact.goal_steering import ordered_evidence
from clio_agent.runtime import trace

__all__ = [
    "EVIDENCE_EXCEEDS_JUDGE_CONTEXT",
    "JUDGE_CONTEXT_WINDOW_UNKNOWN",
    "JudgeRoom",
    "bounded_inputs",
    "judge_room",
]

#: Typed reason: older evidence was left out because it does not fit the judge's window.
EVIDENCE_EXCEEDS_JUDGE_CONTEXT = "evidence_exceeds_judge_context"
#: Typed reason: the judge's window is unknown, so the evidence is given whole.
JUDGE_CONTEXT_WINDOW_UNKNOWN = "judge_context_window_unknown"

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "token",
        "password",
        "authorization",
        "secret",
        "access_token",
        "refresh_token",
    }
)

_INJECTION_SOURCES = frozenset(
    {"task_results", "goal_judgment", "loop_wake", "scheduled_wake", "task_completion"}
)


@dataclass(frozen=True)
class JudgeRoom:
    """Tokens the judge's transcript may use, and the window they come from."""

    tokens: int
    window: int


def judge_room(lm: Any, *, other_inputs: str) -> JudgeRoom | None:
    """The room for evidence in the judge's window (``None``: the window is unknown).

    Args:
        lm: The judge LM (a CLIO-constructed LM carries its provider config).
        other_inputs: Everything else the judge reads: instructions, goal condition and
            retained progress ledger.
    """
    from clio_agent.gact.runtime.context_tokens import (  # noqa: PLC0415
        _autocompact_threshold,
        _estimate_text_tokens,
        _resolve_expert_context_window,
    )

    config = getattr(lm, "_clio_provider_config", None) or lm
    window = int(_resolve_expert_context_window(config) or 0)
    if window <= 0:
        trace.event("GOAL", "%s model=%s", JUDGE_CONTEXT_WINDOW_UNKNOWN, getattr(lm, "model", ""))
        return None
    output = int(getattr(config, "max_tokens", 0) or 0)
    usable = int(window * _autocompact_threshold())
    if output > 0:
        usable = min(usable, window - output)
    return JudgeRoom(tokens=usable - _estimate_text_tokens(other_inputs), window=window)


def bounded_inputs(value: Any) -> str:
    """Render recorded tool inputs whole, with structured credentials redacted."""

    def clean(item: Any, seen: frozenset[int]) -> Any:
        if isinstance(item, (Mapping, list, tuple)):
            if id(item) in seen:
                return "[recursive reference]"
            seen = seen | {id(item)}
        if isinstance(item, Mapping):
            return {
                str(k): "[redacted]" if str(k).lower() in _SECRET_KEYS else clean(v, seen)
                for k, v in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [clean(v, seen) for v in item]
        if item is None or isinstance(item, (str, bool, int, float)):
            return item
        return str(item)

    return json.dumps(clean(value, frozenset()), ensure_ascii=False)


def _identifier(value: Any) -> str:
    """One recorded identifier (turn, message, tool name) on a single line."""

    return " ".join(str(value or "").split())


def _tool_result_fact(part: Any) -> str:
    """Project the result recorded on a tool part, whole."""

    structured = getattr(part, "structured_content", None)
    raw = (getattr(part, "metadata", None) or {}).get("result")
    if not isinstance(structured, Mapping) and isinstance(raw, Mapping):
        structured = raw.get("structuredContent")
    if isinstance(structured, Mapping) and any(
        key in structured for key in ("exit_code", "stdout", "stderr", "timed_out")
    ):
        # The command text is already on the call; the result's facts are its exit
        # status and its output, which the judge reads in full.
        facts = {key: structured[key] for key in ("exit_code", "timed_out") if key in structured}
        for key in ("stdout", "stderr"):
            text = str(structured.get(key) or "")
            if text:
                facts[key] = text
        return json.dumps(facts, ensure_ascii=False)
    for owner, keys in (
        (getattr(part, "structured_content", None), ("message", "summary", "status")),
        (getattr(part, "presentation", None), ("summary",)),
        (getattr(part, "metadata", None), ("result",)),
    ):
        if not isinstance(owner, Mapping):
            continue
        for key in keys:
            fact = str(owner.get(key) or "").strip()
            if fact:
                return fact
    for child in getattr(part, "content", []) or []:
        fact = str(getattr(child, "text", "") or "").strip()
        if fact:
            return fact
    return ""


def _part_line(role: str, part: Any) -> str:
    """One transcript line for a recorded part (``""`` for parts the judge does not read)."""

    part_type = str(getattr(part, "type", ""))
    if part_type == "text":
        text = str(getattr(part, "text", "") or "").strip()
        return f"{role}: {text}" if text else ""
    if part_type == "injection":
        source = getattr(part, "source", "")
        if source not in _INJECTION_SOURCES:
            return ""
        text = str(getattr(part, "text", "") or "").strip()
        return f"recorded harness injection ({source}): {text}"
    if part_type == "tool_call":
        name = _identifier(getattr(part, "tool_name", ""))
        if not name:
            return ""
        inputs = getattr(part, "input", None)
        suffix = f": {bounded_inputs(inputs)}" if inputs else ""
        return f"{role} tool call: {name}{suffix}"
    if part_type == "tool_result":
        name = _identifier(getattr(part, "tool_name", ""))
        if not name:
            return ""
        outcome = "failed" if bool(getattr(part, "is_error", False)) else "completed"
        fact = _tool_result_fact(part)
        suffix = f": {fact}" if fact else ""
        return f"{role} tool result: {name} {outcome}{suffix}"
    return ""


def _entries(app: Any, sid: str) -> list[str]:
    """Every recorded message as transcript entries, oldest first."""

    store = getattr(app.state, "messages", None)
    if store is None:
        return []
    messages = sorted(store.get(sid, []), key=lambda row: str(getattr(row, "created_at", "")))
    entries: list[str] = []
    for message, parts in ordered_evidence(messages):
        role = str(getattr(message, "role", "") or "")
        turn_id = _identifier(getattr(message, "turn_id", "")) or "unassigned"
        message_id = _identifier(getattr(message, "id", "")) or "unassigned"
        lines = [f"--- {role} turn {turn_id}; message {message_id} ---"]
        lines.extend(line for part in parts if (line := _part_line(role, part)))
        entries.append("\n".join(lines))
    return entries


def _cut(text: str, tokens: int, need: int, *, keep_tail: bool) -> str:
    """Part of ``text`` sized to ``tokens`` of its ``need`` (proportional, marked)."""

    keep = max(0, len(text) * max(tokens, 0) // max(need, 1))
    marker = f"[clio: {len(text) - keep} earlier characters of this entry do not fit]"
    if keep_tail:
        return f"{marker}\n{text[len(text) - keep :]}" if keep else marker
    tail = f"[clio: {len(text) - keep} later characters of the working state do not fit]"
    return f"{text[:keep]}\n{tail}"


def _omission_note(room: JudgeRoom, omitted: int, omitted_tokens: int) -> str:
    """What the judge is told when its window cannot hold every recorded entry."""

    head = (
        f"[clio: {EVIDENCE_EXCEEDS_JUDGE_CONTEXT}] The recorded evidence is larger than "
        f"your context window ({room.window} tokens), so this transcript holds the most "
        "recent evidence"
    )
    if not omitted:
        return f"{head}: only the most recent part of the newest entry fits."
    return (
        f"{head}; the {omitted} earliest entries (about {omitted_tokens} tokens) are not "
        "shown. Your previous progress ledger records what earlier judgments verified."
    )


def _fit(working: str, entries: list[str], room: JudgeRoom, sid: str) -> str:
    """The working state and the newest whole entries that fit ``room``, with a note."""
    from clio_agent.gact.runtime.context_tokens import _estimate_text_tokens  # noqa: PLC0415

    left = room.tokens
    if working:
        need = _estimate_text_tokens(working)
        if need > left:
            trace.event(
                "GOAL",
                "%s session=%s window=%d working_state_tokens=%d",
                EVIDENCE_EXCEEDS_JUDGE_CONTEXT,
                sid,
                room.window,
                need,
            )
            working = _cut(working, left, need, keep_tail=False)
        left -= need
    kept: list[str] = []
    for index in range(len(entries) - 1, -1, -1):
        need = _estimate_text_tokens(entries[index]) + 1
        if need <= left:
            kept.append(entries[index])
            left -= need
            continue
        if not kept and left > 0:
            kept.append(_cut(entries[index], left, need, keep_tail=True))
            index -= 1
        omitted = index + 1
        omitted_tokens = sum(_estimate_text_tokens(entry) for entry in entries[:omitted])
        trace.event(
            "GOAL",
            "%s session=%s window=%d room=%d omitted_entries=%d omitted_tokens=%d",
            EVIDENCE_EXCEEDS_JUDGE_CONTEXT,
            sid,
            room.window,
            room.tokens,
            omitted,
            omitted_tokens,
        )
        kept.append(_omission_note(room, omitted, omitted_tokens))
        break
    body = "\n".join(reversed(kept))
    return f"{working}\n\n{body}" if working else body


def _build_transcript(app: Any, sid: str, *, room: JudgeRoom | None = None) -> str:
    """Render the session's recorded evidence for the completion judge.

    Args:
        app: The GACT app (its message and session stores).
        sid: The session the goal belongs to.
        room: The judge's room (:func:`judge_room`); ``None`` gives everything whole.
    """

    entries = _entries(app, sid)
    working = _working_state(app, sid)
    if room is not None:
        return _fit(working, entries, room, sid)
    body = "\n".join(entries)
    return f"{working}\n\n{body}" if working else body


def _working_state(app: Any, sid: str) -> str:
    """The session's plan (with its status) and todos, ahead of the transcript."""
    from clio_agent.gact.session_plan import plan_state  # noqa: PLC0415
    from clio_agent.gact.todos import _render_checklist, recorded_todos  # noqa: PLC0415

    sessions = getattr(app.state, "sessions", None)
    session = sessions.get(sid) if sessions is not None else None
    if session is None:
        return ""
    parts = []
    plan = plan_state(session)
    if plan["exists"]:
        parts.append(f"--- session plan ({plan['status'] or 'no status'}) ---\n{plan['text']}")
    todos = recorded_todos(session)
    if todos:
        parts.append(f"--- todos ---\n{_render_checklist(todos)}")
    return "\n\n".join(parts)
