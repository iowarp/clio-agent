"""SPOTTER review tools: what the watched session did, and what an answer attended to.

Both tools run inside an armed watcher child and read ONLY its watched parent
session, resolved from the controlled session (``spotter_parent_session``
metadata, else the spawned child's parent) — never from a model argument — and
authorized through Core ``read`` relationships (the arm-time SPOTTER grant).
Every result is bounded and model-shaped; every refusal is a typed dict with a
reason, a message and a next-step ``hint`` (never a silent empty answer).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

#: Bounds on review output (items per kind, characters per field).
MAX_ITEMS = 50
MAX_FIELD_CHARS = 500
#: A user request is the evidence of intent *and* of the data it supplied (a pasted
#: table, a replayed tool output), so it is kept whole up to this bound...
MAX_REQUEST_CHARS = 8000
#: ...while the newest requests of one review fit this total; older ones fall back
#: to ``MAX_FIELD_CHARS``. Every clipped request says so (``truncated``/``chars``).
MAX_REQUEST_BUDGET = 24000
#: Bounds on embedded-instruction leads (entries per review, context per side).
MAX_EMBEDDED = 20
INSTRUCTION_CONTEXT = 200
#: The default attention selection (an omitted ``text``) is at most this long.
MAX_SELECTION_CHARS = 4000
#: Bounds on attention output.
TOP_SECTIONS = 6
TOP_BLOCKS = 5
TOP_EXCERPTS = 3
EXCERPT_CONTEXT = 60
EXCERPT_CHARS = 300
#: Runs closer than this many characters merge into one excerpt window.
_RUN_GAP = 24
#: Tools whose ``path``-shaped input records a workspace write.
_WRITE_TOOLS = frozenset({"fs_write_file", "fs_edit_file", "connected_data_write"})
_UNAVAILABLE_HINT = (
    "Attention evidence is unavailable for this answer; judge it from review_activity and "
    "the spotter_* provenance tools instead."
)
_ATTENTION_HINTS: dict[str, str] = {
    "selection_not_located": (
        "The text must be quoted exactly from the assistant's answer or its reasoning "
        "(one contiguous passage), not from the user's message or a tool result. "
        "Omit text (pass only message_id from review_activity.assistant_answers) to "
        "explain the whole answer."
    ),
    "selection_ambiguous": "Quote a longer, unique passage or pass message_id.",
    "message_not_found": "Pass a message_id from review_activity.assistant_answers.",
    "message_not_generated": "Only assistant answers have attention; pass an assistant message_id.",
    "selection_too_large": "Quote a shorter passage (a sentence or a claim).",
    "content_revision_changed": "The answer changed; call review_activity again and re-quote.",
}


def _clip(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _error(reason: str, message: str, hint: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "error": reason, "message": message, "hint": hint, **extra}


def _watched_session() -> tuple[Any, str, str] | dict[str, Any]:
    """Resolve ``(app, caller_sid, watched_sid)`` or a typed refusal."""
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.task_access import TaskAccess  # noqa: PLC0415

    app, sid = context.active_app(), context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    if session is None:
        return _error(
            "no_active_session",
            "SPOTTER review tools run only inside an active watcher session.",
            "Call this from the armed SPOTTER watcher.",
        )
    watched = str(
        (session.metadata or {}).get("spotter_parent_session") or session.parent_session_id or ""
    )
    if not watched:
        return _error(
            "not_a_watcher",
            "This session watches no parent session.",
            "Only an armed SPOTTER watcher (or a spawned reviewer) can review its parent.",
        )
    access = TaskAccess(app.state.task_history, app.state.conversations.caller(sid))
    if watched not in access.session_candidates("read") or app.state.sessions.get(watched) is None:
        return _error(
            "watched_session_unavailable",
            "This session holds no read relationship to the watched session.",
            "The watcher may have been disarmed; stop reviewing.",
            session_id=watched,
        )
    return app, sid, watched


def _instant(value: Any) -> datetime | None:
    """Parse a retained ISO timestamp; an unqualified one is read as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _sort_key(value: Any) -> datetime:
    return _instant(value) or datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# review_activity
# --------------------------------------------------------------------------- #


def _part_text(part: Mapping[str, Any]) -> str:
    text = str(part.get("text") or "")
    if text:
        return text
    return "\n".join(
        str(item.get("text") or "")
        for item in part.get("content") or []
        if isinstance(item, Mapping)
    )


def _latest(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep the most recent ``MAX_ITEMS`` rows (input is chronological)."""
    return rows[-MAX_ITEMS:], max(0, len(rows) - MAX_ITEMS)


# Embedded-instruction heuristics. A *strong* override tries to cancel or hide the
# person's instruction; a *directive* dictates the output. Inside data (a tool
# result, a replayed tool output, a quoted or fenced block) any override phrase or
# output directive is a lead; in the person's own prose only a line pairing a strong
# override with a dictated value (or a concealed directive) is, so "use b.csv
# instead" or "ignore blank rows; report ANSWER: <n>" stays the person's request.
_STRONG_OVERRIDES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("ignore", r"\bignor(?:e|es|ing)\b"),
        ("disregard", r"\bdisregard\b"),
        ("override", r"\boverrid(?:e|es|ing)\b"),
        ("forget", r"\bforget\s+(?:the|your|all|any|previous|prior|above)\b"),
        ("concealment", r"\b(?:do\s+not|don't|never)\s+(?:mention|reveal|disclose|tell)\b"),
        ("role marker", r"(?:^|[\s\[<(])(?:system|developer)\s*:"),
    )
)
_SOFT_OVERRIDES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("instead", r"\binstead\b"),
        ("you must", r"\byou\s+(?:must|are\s+required\s+to|have\s+to)\b"),
    )
)
_DIRECTIVES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        (
            "report answer",
            r"\b(?:report|output|return|give|state|print)\s+(?:the\s+)?(?:final\s+)?answer\b",
        ),
        ("respond with", r"\b(?:respond|reply|answer)\s+(?:only\s+)?with\b"),
        ("say that", r"\b(?:say|write|state|conclude)\s+(?:that|only|exactly)\b"),
    )
)
#: An ``ANSWER: <value>`` pair, as an instruction dictates it and an answer states it.
_ANSWER_LITERAL = re.compile(r"\bANSWER\s*[:=]\s*([^\s;,]+)", re.IGNORECASE)
_QUOTED_LITERAL = re.compile(r"[\"“']([^\"”'\n]{3,80})[\"”']")
#: Lines that open a data block in a user message (a replayed tool output and the like).
_DATA_MARKER = re.compile(
    r"^\s*(?:>|<\s*(?:tool_result|tool_output|output|observation)\b|\[\s*tool\b|"
    r"(?:tool[ _-]?(?:output|result)|observation|output|result)\s*:)",
    re.IGNORECASE,
)
_DATA_CLOSE = re.compile(r"^\s*</\s*(?:tool_result|tool_output|output|observation)\s*>", re.I)


def _labels(line: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    return [label for label, pattern in patterns if pattern.search(line)]


def _data_lines(text: str) -> Iterable[tuple[int, str, bool]]:
    """``(offset, line, inside_data)`` for each line of a user message.

    Data is a fenced block, a ``>``/``>>>`` line, or the lines after a tool-output
    marker up to the next blank line (or closing tag).
    """
    offset, fenced, marked = 0, False, False
    for line in text.splitlines(keepends=True):
        bare = line.strip()
        if bare.startswith("```"):
            fenced = not fenced
            yield offset, line, True
        elif not bare:
            marked = False
        elif _DATA_CLOSE.match(line):
            marked = False
            yield offset, line, True
        else:
            if _DATA_MARKER.match(line):
                marked = True
            yield offset, line, fenced or marked or bare.startswith(">")
        offset += len(line)


def _instruction_hit(line: str, in_data: bool) -> str:
    """Why ``line`` reads as an embedded instruction (``''`` when it does not)."""
    strong, soft = _labels(line, _STRONG_OVERRIDES), _labels(line, _SOFT_OVERRIDES)
    directives = _labels(line, _DIRECTIVES)
    # In the person's prose the directive must dictate a concrete value ("report
    # ANSWER: 73", not the format line "report ANSWER: <n>"), or hide itself.
    hidden = bool({"concealment", "role marker"} & set(strong)) and bool(directives)
    if not in_data and strong and (_dictated_literals(line) or hidden):
        found = strong + (directives or ["answer literal"])
        return "line pairs an override with an output directive: " + ", ".join(found)
    if in_data and (strong or soft or directives):
        return "instruction phrase inside supplied data: " + ", ".join(strong + soft + directives)
    return ""


def _dictated_literals(line: str) -> list[str]:
    """Literal values an instruction line dictates (``ANSWER: 73``, a quoted phrase)."""
    literals = []
    for match in _ANSWER_LITERAL.finditer(line):
        value = match.group(1).rstrip(".")
        if value and not value.startswith("<"):
            literals.append(f"ANSWER: {value}")
    if _labels(line, _DIRECTIVES):
        literals.extend(m.group(1).strip() for m in _QUOTED_LITERAL.finditer(line))
    return literals


def _literal_span(answer: str, literal: str) -> tuple[int, int] | None:
    if literal.startswith("ANSWER: "):
        value = re.escape(literal.removeprefix("ANSWER: "))
        found = re.search(rf"\bANSWER\s*[:=]\s*{value}(?![\w]|\.\d)", answer, re.IGNORECASE)
    else:
        found = re.search(re.escape(literal), answer)
    return found.span() if found else None


def _context(text: str, lo: int, hi: int, width: int = INSTRUCTION_CONTEXT) -> str:
    start, end = max(0, lo - width), min(len(text), hi + width)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def _offsets(text: str) -> Iterable[tuple[int, str]]:
    offset = 0
    for line in text.splitlines(keepends=True):
        yield offset, line
        offset += len(line)


def scan_embedded_instructions(
    text: str, *, message_id: str, source: str, call_id: str = ""
) -> list[dict[str, Any]]:
    """Instruction-like passages inside ``text`` (pure, deterministic; see review_activity).

    ``source`` is ``"user_request"`` (only data blocks, or a line pairing a strong
    override with a directive, count) or ``"tool_result"`` (the whole text is data).
    """
    rows = []
    lines = (
        _data_lines(text)
        if source == "user_request"
        else ((offset, line, True) for offset, line in _offsets(text))
    )
    for offset, line, in_data in lines:
        reason = _instruction_hit(line, in_data)
        if not reason:
            continue
        lo, hi = offset, offset + len(line.rstrip("\r\n"))
        rows.append(
            {
                "message_id": message_id,
                "source": source,
                **({"call_id": call_id} if call_id else {}),
                "offset": lo,
                "instruction": _clip(line.strip()),
                "excerpt": _context(text, lo, hi),
                "reason": reason,
                "dictates": _dictated_literals(line),
            }
        )
    return rows


def _answer_leads(
    instructions: list[tuple[int, dict[str, Any]]], answers: list[tuple[int, str, str]]
) -> list[dict[str, Any]]:
    """Answers that state a literal an earlier embedded instruction dictated."""
    leads = []
    for answer_index, answer_id, answer in answers:
        for index, row in instructions:
            if index > answer_index:
                continue
            for literal in row["dictates"]:
                span = _literal_span(answer, literal)
                if span is None:
                    continue
                leads.append(
                    {
                        "kind": "answer_matches_embedded_instruction",
                        "message_id": answer_id,
                        "literal": literal,
                        "instruction_message_id": row["message_id"],
                        "instruction_source": row["source"],
                        "instruction_excerpt": row["instruction"],
                        "answer_excerpt": _context(answer, *span, width=80),
                    }
                )
                break
    return leads


_ANSWER_LINE = re.compile(
    r"^[^\S\n]*(\S.*?\bANSWER\s*[:=].*?|ANSWER\s*[:=].*?)[^\S\n]*$", re.M | re.I
)


def _answer_line_span(text: str) -> tuple[int, int] | None:
    """The span of the last ``... ANSWER: ...`` line of an answer, if any."""
    found = list(_ANSWER_LINE.finditer(text))
    return found[-1].span(1) if found else None


def _answer_line_field(text: str) -> dict[str, str]:
    """``answer_line`` when the clipped answer text would hide it."""
    span = _answer_line_span(text)
    if span is None or span[1] <= MAX_FIELD_CHARS - 1:
        return {}
    return {"answer_line": _clip(text[span[0] : span[1]])}


def _bound_requests(rows: list[dict[str, Any]]) -> None:
    """Keep the newest requests whole (up to the per-request bound and review budget)."""
    budget = MAX_REQUEST_BUDGET
    for row in reversed(rows):
        text = row.pop("_full")
        limit = min(MAX_REQUEST_CHARS, max(MAX_FIELD_CHARS, budget))
        row["text"] = _clip(text, limit)
        row["chars"] = len(text)
        row["truncated"] = len(text) > limit
        budget -= len(row["text"])


def summarize_activity(
    messages: list[dict[str, Any]], events: list[dict[str, Any]], since: str = ""
) -> dict[str, Any]:
    """Project retained messages + semantic events newer than ``since`` (pure).

    Args:
        messages: The watched session's messages as JSON dicts (``model_dump``).
        events: Its durable semantic events (``session_export._events``).
        since: An ISO timestamp; only strictly newer items are returned.

    User requests are kept whole up to ``MAX_REQUEST_CHARS`` (they are the
    evidence of intent and of the data the person supplied); a clipped one says
    so with ``truncated`` and its full ``chars``. Other text fields are clipped
    to ``MAX_FIELD_CHARS``.

    ``embedded_instructions`` lists instruction-like passages found inside
    supplied data: a tool result, or a data block of a user message (a fenced or
    ``>``/``>>>`` block, the lines after a tool-output marker), plus any user line
    pairing a strong override ("ignore", "override", "do not mention", ...) with
    an output directive ("report ANSWER", "respond with", ...). Each has
    ``message_id``, ``source`` (``user_request`` | ``tool_result``), the
    ``instruction`` line, an ``excerpt`` (±``INSTRUCTION_CONTEXT`` chars), a
    ``reason`` and the literals it ``dictates``. ``leads`` then cross-checks the
    answers: an answer stating a literal an earlier embedded instruction dictated
    (``ANSWER: 73`` in both) is a ``answer_matches_embedded_instruction`` lead.
    Both are deterministic text heuristics run over full (unclipped) texts: a
    lead to check with ``attention_sources`` and the record, never a verdict. A
    replayed tool output pasted into a user message is data the person supplied,
    not the person's instruction.

    Returns:
        Bounded ``user_requests`` / ``assistant_answers`` / ``tool_calls`` /
        ``writes`` / ``lm_calls`` / ``embedded_instructions`` / ``leads`` plus
        ``next_since`` (the newest timestamp seen, to pass back as ``since`` on
        the next review) and ``truncated`` counts.

    Raises:
        ValueError: ``since`` is not an ISO timestamp.
    """
    from clio_agent.gact.session_export import _tool_records  # noqa: PLC0415

    floor = _instant(since) if since.strip() else None
    if since.strip() and floor is None:
        raise ValueError(f"since is not an ISO timestamp: {since!r}")
    newest: tuple[datetime, str] | None = None

    def fresh(stamp: Any) -> bool:
        nonlocal newest
        instant = _instant(stamp)
        if instant is None:
            return floor is None
        if newest is None or instant > newest[0]:
            newest = (instant, str(stamp))
        return floor is None or instant > floor

    requests: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    call_stamp: dict[str, str] = {}
    call_tool: dict[str, str] = {}
    call_output: dict[str, str] = {}
    answer_by_turn: dict[str, str] = {}
    # Instructions are scanned over every message (an answer may follow one an
    # earlier review already reported); only fresh ones are listed again.
    instructions: list[tuple[int, dict[str, Any]]] = []
    fresh_instructions: list[dict[str, Any]] = []
    fresh_answers: list[tuple[int, str, str]] = []
    for index, message in enumerate(messages):
        created = message.get("created_at", "")
        mid = str(message.get("id") or "")
        parts = [p for p in message.get("parts") or [] if isinstance(p, Mapping)]
        found: list[dict[str, Any]] = []
        for part in parts:
            cid = str(part.get("call_id") or part.get("id") or "")
            if part.get("type") == "tool_call" and cid:
                call_stamp.setdefault(cid, created)
                call_tool[cid] = str(part.get("tool_name") or part.get("name") or "")
            elif part.get("type") == "tool_result" and cid:
                call_output.setdefault(cid, _part_text(part))
            if part.get("type") == "tool_result":
                found += scan_embedded_instructions(
                    _part_text(part), message_id=mid, source="tool_result", call_id=cid
                )
        role = message.get("role")
        if role == "assistant" and message.get("turn_id"):
            answer_by_turn[str(message["turn_id"])] = mid
        text = "\n".join(_part_text(p) for p in parts if p.get("type") == "text").strip()
        if role == "user" and text:
            found = scan_embedded_instructions(text, message_id=mid, source="user_request") + found
        instructions += [(index, row) for row in found]
        # A streamed answer keeps its creation time while it grows; its last
        # update is what makes it new to a review cursor.
        if not fresh(message.get("updated_at") or created):
            continue
        fresh_instructions += found
        if role == "user" and text:
            requests.append({"message_id": message.get("id"), "created_at": created, "_full": text})
        elif role == "assistant":
            if text:
                fresh_answers.append((index, mid, text))
            thinking = any(
                p.get("type") == "thinking" or (p.get("type") == "tool_call" and p.get("thought"))
                for p in parts
            )
            if text or thinking:
                answers.append(
                    {
                        "message_id": message.get("id"),
                        "created_at": created,
                        "text": _clip(text),
                        "has_thinking": thinking,
                        **_answer_line_field(text),
                    }
                )
    stamps = {str(e.get("event_id")): str(e.get("occurred_at") or "") for e in events}
    tool_rows: list[dict[str, Any]] = []
    for row in _tool_records(messages, events):
        cid = str(row.get("call_id") or "")
        observed = [stamps.get(str(eid), "") for eid in row.get("event_ids", [])]
        stamp = max(observed, key=_sort_key, default="") or call_stamp.get(cid, "")
        if not fresh(stamp):
            continue
        tool = str(row.get("tool") or call_tool.get(cid, ""))
        output = row.get("output", call_output.get(cid))
        entry = {
            "call_id": cid,
            "tool": tool,
            "status": str(row.get("status") or "recorded"),
            "at": stamp,
            "input": _clip(row.get("input", {})),
            "output": _clip(output) if output not in (None, "") else "",
            **({"error": _clip(row["error"])} if row.get("error") else {}),
        }
        tool_rows.append(entry)
        args = row.get("input") if isinstance(row.get("input"), Mapping) else {}
        if tool in _WRITE_TOOLS and args.get("path"):
            writes.append({"source": tool, "path": str(args["path"]), "call_id": cid, "at": stamp})
    lm_rows: list[dict[str, Any]] = []
    for event in events:
        kind = str(event.get("event_type") or "")
        if not kind.startswith(("artifact.", "lm.call")):
            continue
        stamp = event.get("occurred_at", "")
        if not fresh(stamp):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if kind == "lm.call":
            turn = str(event.get("turn_id") or "")
            lm_rows.append(
                {
                    "lm_call_id": str(event.get("event_id") or ""),
                    "response_id": str(payload.get("response_id") or ""),
                    "turn_id": turn,
                    "message_id": answer_by_turn.get(turn, ""),
                    "model": str(payload.get("model") or ""),
                    "at": stamp,
                }
            )
        elif kind in {"artifact.created", "artifact.version.added"}:
            subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
            path = payload.get("path") or payload.get("uri") or subject.get("path") or ""
            name = payload.get("name") or subject.get("name") or ""
            writes.append(
                {
                    "source": kind,
                    "path": _clip(str(path or name), 300),
                    "artifact_id": str(payload.get("artifact_id") or payload.get("id") or ""),
                    "at": stamp,
                }
            )
    out: dict[str, Any] = {"since": since, "truncated": {}}
    for key, rows in (
        ("user_requests", requests),
        ("assistant_answers", answers),
        ("tool_calls", sorted(tool_rows, key=lambda row: _sort_key(row["at"]))),
        ("writes", sorted(writes, key=lambda row: _sort_key(row["at"]))),
        ("lm_calls", lm_rows),
    ):
        out[key], dropped = _latest(rows)
        if dropped:
            out["truncated"][key] = dropped
    _bound_requests(out["user_requests"])
    leads = _answer_leads(instructions, fresh_answers)
    reported = {(r["message_id"], r["offset"], r.get("call_id", "")) for r in fresh_instructions}
    # An answer that follows an instruction an earlier review reported lists it again.
    for _index, row in instructions:
        key = (row["message_id"], row["offset"], row.get("call_id", ""))
        if key not in reported and any(
            lead["instruction_message_id"] == row["message_id"]
            and lead["instruction_excerpt"] == row["instruction"]
            for lead in leads
        ):
            fresh_instructions.append(row)
            reported.add(key)
    for key, rows in (("embedded_instructions", fresh_instructions), ("leads", leads)):
        out[key] = rows[:MAX_EMBEDDED]
        if len(rows) > MAX_EMBEDDED:
            out["truncated"][key] = len(rows) - MAX_EMBEDDED
    out["next_since"] = newest[1] if newest is not None else since
    return out


def review_activity_result(since: str = "") -> dict[str, Any]:
    """Run ``review_activity`` for the active watcher (see :func:`build_review_activity_tool`)."""
    from clio_agent.gact import session_export  # noqa: PLC0415

    resolved = _watched_session()
    if isinstance(resolved, dict):
        return resolved
    app, _caller, watched = resolved
    messages = [
        m.model_dump(mode="json", exclude_none=True) for m in app.state.messages.get(watched, [])
    ]
    events, source = session_export._events(app, watched)
    try:
        result = summarize_activity(messages, events, since)
    except ValueError as exc:
        return _error(
            "invalid_argument", str(exc), "Pass since exactly as the previous next_since, or ''."
        )
    return {"session_id": watched, "recording": source, **result}


# --------------------------------------------------------------------------- #
# attention_sources
# --------------------------------------------------------------------------- #


def _locate_message(messages: Iterable[Any], text: str) -> str:
    """The latest assistant message whose answer text or thought renders ``text``."""
    from clio_agent.gact.attention.rendered import find_rendered  # noqa: PLC0415

    for message in reversed(list(messages)):
        if getattr(message, "role", "") != "assistant":
            continue
        for part in getattr(message, "parts", []) or []:
            if getattr(part, "type", "") not in {"text", "thinking", "tool_call"}:
                continue
            for source in (getattr(part, "text", ""), getattr(part, "thought", "")):
                if source and find_rendered(source, text) is not None:
                    return str(message.id)
    return ""


def _windows(runs: list[list[float]]) -> list[tuple[int, int, float]]:
    """Merge ``[char_lo, char_hi, value]`` heat runs into nearby windows, value-summed."""
    windows: list[list[float]] = []
    for lo, hi, value in sorted(runs, key=lambda r: r[0]):
        if windows and lo - windows[-1][1] <= _RUN_GAP:
            windows[-1][1] = max(windows[-1][1], hi)
            windows[-1][2] += value
        else:
            windows.append([lo, hi, value])
    return [(int(lo), int(hi), float(v)) for lo, hi, v in windows]


def _excerpt(text: str, lo: int, hi: int) -> str:
    start, end = max(0, lo - EXCERPT_CONTEXT), min(len(text), hi + EXCERPT_CONTEXT)
    body = ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")
    return _clip(body, EXCERPT_CHARS)


def compact_attention(payload: Mapping[str, Any], messages: list[Any]) -> dict[str, Any]:
    """Reduce an ``explain_selection`` payload to labelled sources and hot excerpts (pure)."""
    from clio_agent.gact.attention.transcript_map import transcript_texts  # noqa: PLC0415

    texts = {(t.message_id, t.part_id, t.field): t.text for t in transcript_texts(messages)}
    selection = payload.get("selection") or {}
    blocks = []
    for block in sorted(payload.get("blocks") or [], key=lambda b: -float(b.get("share", 0.0)))[
        :TOP_BLOCKS
    ]:
        text = block.get("source_text") or texts.get(
            (block.get("message_id"), block.get("part_id"), block.get("field")), ""
        )
        hot = sorted(_windows(block.get("runs") or []), key=lambda w: -w[2])[:TOP_EXCERPTS]
        blocks.append(
            {
                "kind": block.get("kind", ""),
                "message_id": block.get("message_id", ""),
                "part_id": block.get("part_id", ""),
                **({"call_id": block["call_id"]} if block.get("call_id") else {}),
                "share": round(float(block.get("share", 0.0)), 4),
                "excerpts": [
                    {"text": _excerpt(text, lo, hi), "share": round(value, 4)}
                    for lo, hi, value in hot
                    if text
                ],
            }
        )
    sections = sorted(payload.get("sections") or [], key=lambda s: -float(s.get("share", 0.0)))
    return {
        "available": True,
        "selection": {
            "message_id": payload.get("message_id", ""),
            "part_id": selection.get("part_id", ""),
            "field": selection.get("field", ""),
            "text": _clip(selection.get("text", ""), EXCERPT_CHARS),
            "steps": selection.get("steps", []),
            "step_count": selection.get("step_count", 0),
        },
        "sources": [
            {"domain": s["domain"], "share": round(float(s["share"]), 4)}
            for s in sorted(payload.get("sources") or [], key=lambda s: -float(s["share"]))
        ],
        "sections": [
            {
                "domain": s.get("domain", ""),
                "label": s.get("label", ""),
                "share": round(float(s.get("share", 0.0)), 4),
            }
            for s in sections[:TOP_SECTIONS]
        ],
        "blocks": blocks,
        "flags": list(payload.get("flags") or []),
        "residual": round(float(payload.get("residual") or 0.0), 4),
        "evidence": {
            "response_id": payload.get("response_id", ""),
            "capture_sha256": payload.get("capture_sha256", ""),
            "steps": list(payload.get("selected_steps") or []),
            "profile": payload.get("profile"),
            "profile_revision": payload.get("profile_revision", ""),
            "lm_call_id": payload.get("lm_call_id", ""),
        },
        "note": (
            "Attention shows what the answer weighted, not why. Pass `evidence` (plus your "
            "`uncertainty`) to raise_alert_card's attention_evidence; it is not proof alone."
        ),
    }


def _in_user_message(messages: Iterable[Any], text: str) -> str:
    """The id of a user message whose text contains ``text`` (``''`` if none)."""
    needle = " ".join(text.split())
    for message in messages:
        if getattr(message, "role", "") != "user":
            continue
        for part in getattr(message, "parts", []) or []:
            if needle and needle in " ".join(str(getattr(part, "text", "") or "").split()):
                return str(message.id)
    return ""


def default_selection(messages: list[Any], message_id: str = "") -> dict[str, Any]:
    """What to explain when ``text`` is omitted (pure).

    The answer's last ``ANSWER:`` line when it has one, else its whole final text
    (else the reasoning behind its last action), bounded to the last
    ``MAX_SELECTION_CHARS``. ``message_id`` empty means the latest assistant
    message with generated text. Returns the ``SelectionRequest`` fields plus a
    ``basis``, or a typed error.
    """
    from clio_agent.gact.attention.transcript_map import transcript_texts  # noqa: PLC0415

    wanted = str(message_id or "").strip()
    if wanted:
        candidates = [m for m in messages if str(getattr(m, "id", "")) == wanted]
        if not candidates:
            return _error(
                "message_not_found",
                f"No message {wanted!r} in the watched session.",
                _ATTENTION_HINTS["message_not_found"],
            )
        if getattr(candidates[0], "role", "") != "assistant":
            return _error(
                "message_not_generated",
                f"{wanted!r} is a {getattr(candidates[0], 'role', '')} message; it has no "
                "attention.",
                _ATTENTION_HINTS["message_not_generated"],
            )
    else:
        candidates = [m for m in reversed(messages) if getattr(m, "role", "") == "assistant"]
    for message in candidates:
        texts = transcript_texts([message])
        answers = [t for t in texts if t.kind == "assistant_text" and t.text.strip()]
        reasons = [t for t in texts if t.kind in {"thought", "reasoning"} and t.text.strip()]
        chosen = answers[-1] if answers else (reasons[-1] if reasons else None)
        if chosen is None:
            continue
        body = chosen.text
        span = _answer_line_span(body) if answers else None
        if span is not None:
            basis = "answer_line"
        else:
            lo = len(body) - len(body.lstrip())
            span, basis = (lo, len(body.rstrip())), "answer" if answers else "reasoning"
        lo, hi = span
        if hi - lo > MAX_SELECTION_CHARS:
            lo, basis = hi - MAX_SELECTION_CHARS, f"{basis}_tail"
        return {
            "message_id": str(message.id),
            "part_id": chosen.part_id,
            "field": chosen.field,
            "start": lo,
            "end": hi,
            "basis": basis,
        }
    return _error(
        "message_not_generated",
        "No assistant answer with generated text in the watched session"
        + (f" ({wanted!r})." if wanted else "."),
        "Call review_activity and pass a message_id from assistant_answers.",
    )


def attention_sources_result(text: str = "", message_id: str = "") -> dict[str, Any]:
    """Run ``attention_sources`` for the active watcher (see :func:`build_attention_sources_tool`)."""
    from clio_agent.gact.attention import routes  # noqa: PLC0415
    from clio_agent.gact.attention.reasons import AttentionUnavailable  # noqa: PLC0415
    from clio_agent.gact.attention.service import (  # noqa: PLC0415
        SelectionRequest,
        explain_selection,
    )

    resolved = _watched_session()
    if isinstance(resolved, dict):
        return resolved
    app, _caller, watched = resolved
    quoted = str(text or "").strip()
    messages = list(app.state.messages.get(watched, []))
    if quoted:
        mid = str(message_id or "").strip() or _locate_message(messages, quoted)
        if not mid:
            user_mid = _in_user_message(messages, quoted)
            return _error(
                "selection_not_located",
                (
                    f"That text is in the user's message {user_mid!r}, not in an assistant "
                    "answer; user messages have no attention."
                    if user_mid
                    else "No assistant answer in the watched session contains that text."
                ),
                _ATTENTION_HINTS["selection_not_located"],
                session_id=watched,
            )
        request, basis = SelectionRequest(message_id=mid, text=quoted), "quoted"
    else:
        chosen = default_selection(messages, message_id)
        if chosen.get("error"):
            return {**chosen, "session_id": watched}
        basis = chosen.pop("basis")
        request = SelectionRequest(**chosen)
        mid = request.message_id
    try:
        payload = explain_selection(
            messages=messages,
            calls=routes.session_lm_calls(app, watched),
            store=routes.attention_store(app),
            renderer_for=routes._renderer,
            request=request,
        )
    except AttentionUnavailable as exc:
        wire = exc.to_wire()
        return _error(
            str(wire["reason"]),
            str(wire["message"]),
            _ATTENTION_HINTS.get(str(wire["reason"]), _UNAVAILABLE_HINT),
            detail=_clip(wire.get("detail") or "", 300),
            session_id=watched,
            message_id=mid,
        )
    out = {"session_id": watched, **compact_attention(payload, messages)}
    out["selection"]["basis"] = basis
    return out


def build_review_activity_tool(agent_def: Any = None) -> Any:
    """Build ``review_activity`` (claimed only by the SPOTTER plugin)."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def review_activity(since: str = "") -> dict[str, Any]:
        return review_activity_result(since)

    return native_tool(
        review_activity,
        name="review_activity",
        presentation="specialized",
        domain="memory",
        title="Review watched activity",
        representation="row",
        desc=(
            "Read what the watched session did since `since` (ISO; '' = from the start): the "
            "user's requests (whole, up to 8000 chars), assistant answers (message_id, excerpt, "
            "has_thinking), tool calls (input/output excerpts, errors), writes (paths/artifacts), "
            "model calls, embedded_instructions (instruction-like text inside supplied data or "
            "tool results) and leads (an answer stating what an embedded instruction dictated). "
            "Leads are heuristics, not verdicts. Pass the returned next_since as `since` on your "
            "next review."
        ),
        args={
            "since": {
                "type": "string",
                "description": "Previous next_since (ISO timestamp); empty for everything.",
            }
        },
        read_only=True,
    )


def build_attention_sources_tool(agent_def: Any = None) -> Any:
    """Build ``attention_sources`` (claimed only by the SPOTTER plugin)."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def attention_sources(text: str = "", message_id: str = "") -> dict[str, Any]:
        return attention_sources_result(text, message_id)

    return native_tool(
        attention_sources,
        name="attention_sources",
        presentation="specialized",
        domain="alerts",
        title="Attention sources",
        representation="row",
        desc=(
            "Show what one passage of the watched session's answer attended to: source shares "
            "by domain (user, tool_result, system, ...), the top labelled prompt sections, and "
            "the hottest excerpts of the transcript blocks it weighted. Simplest: pass only "
            "`message_id` (from review_activity.assistant_answers) to explain that answer's "
            "ANSWER: line (else its whole text); omit both for the latest answer. `text`, when "
            "given, must quote the assistant's answer or reasoning exactly, never the user's "
            "message. Returns `evidence` for raise_alert_card's attention_evidence. Attention "
            "alone is not proof."
        ),
        args={
            "text": {
                "type": "string",
                "description": (
                    "Optional exact passage of the assistant's answer or reasoning; omit to "
                    "explain the answer's ANSWER: line or whole text."
                ),
            },
            "message_id": {
                "type": "string",
                "description": (
                    "Assistant message id from review_activity; empty = the latest answer "
                    "(containing text, when text is given)."
                ),
            },
        },
        read_only=True,
    )


__all__ = [
    "attention_sources_result",
    "build_attention_sources_tool",
    "build_review_activity_tool",
    "compact_attention",
    "default_selection",
    "review_activity_result",
    "scan_embedded_instructions",
    "summarize_activity",
]
