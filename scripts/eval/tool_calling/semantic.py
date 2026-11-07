"""What a run's agent saw and did, step by step: the semantic record and its reading report.

The run JSON (``collect.normalize_export``) keeps timing and counts. Reading a run for
its semantics needs what that leaves out, all of which the session export already holds:

* per model call (``lm.call``): the context it was sent as new messages (the person's
  text, each ``[clio: <source>]`` injection with its text, each tool result as the
  model read it, with its size), the prompt's shape (system and tool-list size), and
  its usage;
* per step (``react.step.completed``): the visible text, and each call with its
  arguments and its observation;
* per session: turns (the person's message each), loaded skills, compactions with
  their checkpoints, and for a child, its task and its final report.

:func:`semantic_record` builds ``run-N.semantic.json`` from the export (plus the work
state, the exact last prompt and the plan approvals the runner gathered);
:func:`reading_report` renders it as ``run-N.read.md``, one file per run, read against
the evaluation's checklist (``docs/design/sdk/tool-calling-ledger.md``).
"""

from __future__ import annotations

import json
import re
from typing import Any

from scripts.eval.tool_calling import collect

__all__ = ["reading_report", "render_semantics", "semantic_record", "semantic_row"]

_INJECTION = re.compile(r"^\[clio: ([^\]]+)\]")
#: Characters of a text shown in the reading report (the record keeps it whole).
EXCERPT = 400


def _parts_in(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The new context of one model call, part by part, classified."""
    out: list[dict[str, Any]] = []
    for message in messages or []:
        role = str(message.get("role") or "")
        for part in message.get("parts") or []:
            kind = str(part.get("type") or "")
            if kind == "text":
                text = str(part.get("text") or "")
                found = _INJECTION.match(text) if role == "user" else None
                out.append(
                    {"kind": "injection", "source": found.group(1), "chars": len(text),
                     "text": text}
                    if found
                    else {"kind": f"{role}_text", "chars": len(text), "text": text}
                )  # fmt: skip
            elif kind == "tool_result":
                text = "".join(
                    str(c.get("text") or "")
                    for c in part.get("content") or []
                    if isinstance(c, dict)
                )
                out.append({"kind": "tool_result", "name": str(part.get("name") or ""),
                            "is_error": bool(part.get("is_error")), "chars": len(text),
                            "text": text})  # fmt: skip
            elif kind == "tool_call":
                out.append({"kind": "tool_call", "name": str(part.get("name") or ""),
                            "args": part.get("input") or {}})  # fmt: skip
    return out


def _calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for event in events:
        if event.get("event_type") != "lm.call":
            continue
        payload = event.get("payload") or {}
        shape = payload.get("prompt_shape") or {}
        tools = [(str(t[0]), int(t[1])) for t in shape.get("tools") or [] if len(t) >= 2]
        row = collect.lm_call_row(event)
        rows.append(
            {
                "started_at": row["started_at"],
                "duration_ms": row["duration_ms"],
                "usage": row["usage"],
                "prompt_tokens": collect.prompt_tokens(row),
                "system_chars": int(shape.get("system_chars") or 0),
                "tool_chars": sum(size for _, size in tools),
                "tools": [name for name, _ in tools],
                "incoming": _parts_in(payload.get("messages") or []),
                "reply_chars": int(payload.get("content_len") or 0),
                "reasoning_chars": int(payload.get("reasoning_len") or 0),
            }
        )
    return rows


def _steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for event in events:
        if event.get("event_type") != "react.step.completed":
            continue
        payload = event.get("payload") or {}
        calls = []
        for call in payload.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            observation = call.get("observation")
            text = (
                observation
                if isinstance(observation, str)
                else json.dumps(observation, default=str)
            )
            calls.append({"name": str(call.get("name") or ""), "args": call.get("args") or {},
                          "is_error": bool(call.get("is_error")), "output_chars": len(text),
                          "output": text})  # fmt: skip
        rows.append({"index": payload.get("step_index"), "text": str(payload.get("thought") or ""),
                     "is_finish": bool(payload.get("is_finish")), "calls": calls,
                     "at": collect.epoch(event.get("occurred_at"))})  # fmt: skip
    return rows


def _turns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"at": collect.epoch(e.get("occurred_at")),
         "text": str((e.get("payload") or {}).get("text") or "")}
        for e in events
        if e.get("event_type") == "turn.started"
    ]  # fmt: skip


def _last_assistant(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "assistant":
            return "".join(
                str(p.get("text") or "")
                for p in message.get("parts") or []
                if p.get("type") == "text"
            )
    return ""


def _session(transcript: dict[str, Any]) -> dict[str, Any]:
    events = sorted(
        (e for e in transcript.get("semantic_events") or [] if isinstance(e, dict)),
        key=lambda e: collect.epoch(e.get("occurred_at")) or 0.0,
    )
    relationship = transcript.get("relationship") or {}
    session = transcript.get("session") or {}
    return {
        "session_id": str(session.get("id") or relationship.get("session_id") or ""),
        "agent": str((session.get("agent") or {}).get("id") or ""),
        "depth": int(relationship.get("depth") or 0),
        "attribution": str(relationship.get("attribution") or "root"),
        "turns": _turns(events),
        "model_calls": _calls(events),
        "steps": _steps(events),
        "skills": [
            str(s.get("name") or s.get("skill") or "")
            for s in transcript.get("loaded_skills") or []
        ],
        "compactions": collect.compaction_rows(events),
        "final_text": _last_assistant(transcript.get("messages") or []),
    }


def semantic_record(
    export: dict[str, Any],
    *,
    work: dict[str, Any] | None = None,
    plan_text: str = "",
    exact_prompt: dict[str, Any] | None = None,
    plan_answers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The run's semantic record (module docstring), root session first."""
    sessions = [_session(export)] + [_session(child) for child in export.get("children") or []]
    exact = exact_prompt or {}
    return {
        "schema": "clio.eval.semantic.v1",
        "sessions": sessions,
        "work": {
            "plan": plan_text,
            "plan_status": (work or {}).get("plan_status"),
            "todos": (work or {}).get("todos") or [],
            "todo_history": (work or {}).get("todo_history") or [],
        },  # fmt: skip
        "plan_answers": list(plan_answers or []),
        "exact_prompt": {
            "system_chars": len(str(exact.get("system") or "")),
            "system": str(exact.get("system") or ""),
            "tools": [
                str(t.get("name") or "") for t in exact.get("tools") or [] if isinstance(t, dict)
            ],
            # Each listed tool's description and schema, in characters (per-tool cost).
            "tool_chars": {
                str(t.get("name") or ""): len(str(t.get("description") or ""))
                + len(json.dumps(t.get("parameters") or {}))
                for t in exact.get("tools") or []
                if isinstance(t, dict)
            },
        },
    }


def _short(text: str, limit: int = EXCERPT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + f" … (+{len(text) - limit} chars)"


def _args(args: Any) -> str:
    return _short(json.dumps(args, default=str, ensure_ascii=False), 160)


def _pair(calls: list[dict[str, Any]], steps: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """Each model call's step: the first step recorded after the call started and before
    the next call did (a compaction's checkpoint call has none)."""
    starts = [c["started_at"] or 0.0 for c in calls] + [float("inf")]
    left = sorted(steps, key=lambda s: s["at"] or 0.0)
    out: list[dict[str, Any] | None] = []
    for number in range(len(calls)):
        match = next(
            (s for s in left if starts[number] <= (s["at"] or 0.0) < starts[number + 1]), None
        )
        if match is not None:
            left.remove(match)
        out.append(match)
    return out


def _session_report(session: dict[str, Any]) -> list[str]:
    lines = [
        f"### {session['attribution']} {session['session_id']} ({session['agent'] or 'agent'})"
    ]
    if session["skills"]:
        lines.append(f"Skills loaded: {', '.join(session['skills'])}")
    calls = session["model_calls"]
    paired = _pair(calls, session["steps"])
    for number, call in enumerate(calls):
        u = call["usage"]
        lines.append(
            f"\n**Call {number}**: prompt {call['prompt_tokens']:,} tok "
            f"(new {u['input_tokens']:,}, cache read {u['cache_read_tokens']:,}), "
            f"out {u['output_tokens']:,}, reasoning {u['reasoning_tokens']:,}; "
            f"system {call['system_chars']:,} ch, {len(call['tools'])} tools {call['tool_chars']:,} ch"
        )
        for part in call["incoming"]:
            if part["kind"] == "injection":
                lines.append(f"- in: injection `{part['source']}` {part['chars']:,} ch")
            elif part["kind"] == "tool_result":
                flag = " ERROR" if part["is_error"] else ""
                lines.append(
                    f"- in: result `{part['name']}`{flag} {part['chars']:,} ch: {_short(part['text'], 160)}"
                )
            elif part["kind"] == "user_text":
                lines.append(f"- in: person {part['chars']:,} ch: {_short(part['text'])}")
        step = paired[number]
        if step is not None:
            if step["text"]:
                lines.append(f"- says: {_short(step['text'])}")
            for c in step["calls"]:
                flag = " ERROR" if c["is_error"] else ""
                lines.append(
                    f"- calls `{c['name']}` {_args(c['args'])} -> {c['output_chars']:,} ch{flag}"
                )
    for row in session["compactions"]:
        lines.append(
            f"\n**Compaction** ({row['trigger']}): {row['before']} -> {row['after']} tok, "
            f"replaced {row['replaced']}. Checkpoint: {_short(row['checkpoint'], 600)}"
        )
    if session["depth"]:
        lines.append(
            f"\nFinal report ({len(session['final_text']):,} ch): {_short(session['final_text'], 800)}"
        )
    return lines


def reading_report(record: dict[str, Any], header: dict[str, Any]) -> str:
    """``run-N.read.md``: the run as a step timeline, then plan, todos and children."""
    lines = [f"# {header.get('task')} run {header.get('run_index')} ({header.get('config')})", ""]
    for key in ("wall_s", "active_s", "grade"):
        if header.get(key) is not None:
            lines.append(f"- {key}: {header[key]}")
    root, *children = record["sessions"]
    for turn in root["turns"]:
        lines.append(f"- turn: {_short(turn['text'], 200)}")
    lines += ["", *_session_report(root)]
    work = record["work"]
    lines += ["", "## Plan and todos", f"Plan status: {work.get('plan_status') or 'none'}"]
    if work.get("plan"):
        lines.append(_short(work["plan"], 1500))
    for todo in work.get("todos") or []:
        lines.append(f"- [{todo.get('status')}] {todo.get('content') or todo.get('text')}")
    for answer in record.get("plan_answers") or []:
        lines.append(
            f"- plan approval answered `{answer.get('choice')}` at {answer.get('after_s')} s"
        )
    if children:
        lines += ["", "## Children"]
        for child in children:
            task = child["turns"][0]["text"] if child["turns"] else ""
            lines += ["", f"Task: {_short(task, 600)}", *_session_report(child)]
    exact = record.get("exact_prompt") or {}
    if exact.get("system_chars"):
        lines += [
            "",
            f"Exact last prompt: system {exact['system_chars']:,} ch, tools: {', '.join(exact['tools'])}",
        ]
    return "\n".join(lines) + "\n"


#: The reserved tool name of a ``tool_calls`` block the engine could not read.
INVALID_BLOCK = "clio_invalid_tool_calls"


def semantic_row(record: dict[str, Any]) -> dict[str, Any]:
    """One run's semantic numbers: protocol failures, outputs, context, work, delegation."""
    sessions = record.get("sessions") or []
    root = sessions[0] if sessions else {}
    calls = [c for s in sessions for step in s.get("steps") or [] for c in step["calls"]]
    outputs = sorted(c["output_chars"] for c in calls if c["name"] != INVALID_BLOCK)
    prompts = [c["prompt_tokens"] for c in root.get("model_calls") or [] if c["prompt_tokens"]]
    injections = [
        p for s in sessions for c in s.get("model_calls") or [] for p in c["incoming"]
        if p["kind"] == "injection"
    ]  # fmt: skip
    work = record.get("work") or {}
    todos = work.get("todos") or []
    return {
        "model_calls": sum(len(s.get("model_calls") or []) for s in sessions),
        "invalid_blocks": sum(1 for c in calls if c["name"] == INVALID_BLOCK),
        "tool_errors": sum(1 for c in calls if c["is_error"] and c["name"] != INVALID_BLOCK),
        "output_median_k": (outputs[len(outputs) // 2] / 1e3) if outputs else 0.0,
        "output_max_k": (outputs[-1] / 1e3) if outputs else 0.0,
        "prompt_growth": (prompts[-1] / prompts[0]) if len(prompts) > 1 else 1.0,
        "injected_k": sum(p["chars"] for p in injections) / 1e3,
        "skills": sum(len(s.get("skills") or []) for s in sessions),
        "children": max(len(sessions) - 1, 0),
        "plan": 1.0 if work.get("plan") else 0.0,
        "todo_writes": float(len(work.get("todo_history") or []) + (1 if todos else 0)),
        "todos_open": float(sum(1 for t in todos if t.get("status") != "completed")),
    }


_SEMANTIC_COLUMNS = (
    ("model calls", "model_calls", "{:.0f}"),
    ("invalid blocks", "invalid_blocks", "{:.0f}"),
    ("tool errors", "tool_errors", "{:.0f}"),
    ("output med k", "output_median_k", "{:.1f}"),
    ("output max k", "output_max_k", "{:.1f}"),
    ("prompt growth", "prompt_growth", "{:.1f}"),
    ("injected k", "injected_k", "{:.1f}"),
    ("skills", "skills", "{:.0f}"),
    ("children", "children", "{:.0f}"),
    ("plan", "plan", "{:.0f}"),
    ("todo writes", "todo_writes", "{:.0f}"),
    ("todos open", "todos_open", "{:.0f}"),
)


def render_semantics(runs: list[dict[str, Any]]) -> str:
    """A table per task of the runs that have a semantic record: medians per config."""
    from collections import defaultdict  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    from statistics import median  # noqa: PLC0415

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        path = Path(str(run.get("_path") or "")).with_suffix(".semantic.json")
        if path.is_file():
            row = semantic_row(json.loads(path.read_text(encoding="utf-8")))
            grouped[str(run.get("task"))][str(run.get("config"))].append(row)
    head = "| config | n | " + " | ".join(name for name, _, _ in _SEMANTIC_COLUMNS) + " |"
    lines = []
    for task in sorted(grouped):
        lines += [f"### {task} (semantics)", "", head, "|" + "---|" * (len(_SEMANTIC_COLUMNS) + 2)]
        for config, rows in sorted(grouped[task].items()):
            cells = [fmt.format(median(r[key] for r in rows)) for _, key, fmt in _SEMANTIC_COLUMNS]
            lines.append(f"| {config} | {len(rows)} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)
