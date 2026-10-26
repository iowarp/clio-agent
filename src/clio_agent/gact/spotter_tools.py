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
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

#: Bounds on review output (items per kind, characters per field).
MAX_ITEMS = 50
MAX_FIELD_CHARS = 500
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
        "Quote the text exactly as it appears in the answer (one contiguous passage), "
        "or pass message_id from review_activity.assistant_answers."
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


def summarize_activity(
    messages: list[dict[str, Any]], events: list[dict[str, Any]], since: str = ""
) -> dict[str, Any]:
    """Project retained messages + semantic events newer than ``since`` (pure).

    Args:
        messages: The watched session's messages as JSON dicts (``model_dump``).
        events: Its durable semantic events (``session_export._events``).
        since: An ISO timestamp; only strictly newer items are returned.

    Returns:
        Bounded ``user_requests`` / ``assistant_answers`` / ``tool_calls`` /
        ``writes`` / ``lm_calls`` plus ``next_since`` (the newest timestamp seen,
        to pass back as ``since`` on the next review) and ``truncated`` counts.

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
    for message in messages:
        created = message.get("created_at", "")
        parts = [p for p in message.get("parts") or [] if isinstance(p, Mapping)]
        for part in parts:
            cid = str(part.get("call_id") or part.get("id") or "")
            if part.get("type") == "tool_call" and cid:
                call_stamp.setdefault(cid, created)
                call_tool[cid] = str(part.get("tool_name") or part.get("name") or "")
            elif part.get("type") == "tool_result" and cid:
                call_output.setdefault(cid, _part_text(part))
        role = message.get("role")
        if role == "assistant" and message.get("turn_id"):
            answer_by_turn[str(message["turn_id"])] = str(message.get("id") or "")
        # A streamed answer keeps its creation time while it grows; its last
        # update is what makes it new to a review cursor.
        if not fresh(message.get("updated_at") or created):
            continue
        text = "\n".join(_part_text(p) for p in parts if p.get("type") == "text").strip()
        if role == "user" and text:
            requests.append(
                {"message_id": message.get("id"), "created_at": created, "text": _clip(text)}
            )
        elif role == "assistant":
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


def attention_sources_result(text: str, message_id: str = "") -> dict[str, Any]:
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
    if not quoted:
        return _error(
            "invalid_argument",
            "text is required.",
            "Quote the exact passage of the answer (or its reasoning) you want to check.",
        )
    messages = list(app.state.messages.get(watched, []))
    mid = str(message_id or "").strip() or _locate_message(messages, quoted)
    if not mid:
        return _error(
            "selection_not_located",
            "No assistant answer in the watched session contains that text.",
            "Quote the text exactly as it appears, or pass message_id from "
            "review_activity.assistant_answers.",
            session_id=watched,
        )
    try:
        payload = explain_selection(
            messages=messages,
            calls=routes.session_lm_calls(app, watched),
            store=routes.attention_store(app),
            renderer_for=routes._renderer,
            request=SelectionRequest(message_id=mid, text=quoted),
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
    return {"session_id": watched, **compact_attention(payload, messages)}


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
            "user's requests, assistant answers (message_id, excerpt, has_thinking), tool calls "
            "(input/output excerpts, errors), writes (paths/artifacts) and model calls. Pass the "
            "returned next_since as `since` on your next review."
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

    def attention_sources(text: str, message_id: str = "") -> dict[str, Any]:
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
            "the hottest excerpts of the transcript blocks it weighted. `text` must quote the "
            "answer (or its reasoning) exactly; `message_id` (from review_activity) "
            "disambiguates. Returns `evidence` for raise_alert_card's attention_evidence. "
            "Attention alone is not proof."
        ),
        args={
            "text": {"type": "string", "description": "Exact quoted passage of the answer."},
            "message_id": {
                "type": "string",
                "description": "Assistant message id; empty = latest answer containing text.",
            },
        },
        read_only=True,
    )


__all__ = [
    "attention_sources_result",
    "build_attention_sources_tool",
    "build_review_activity_tool",
    "compact_attention",
    "review_activity_result",
    "summarize_activity",
]
