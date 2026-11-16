"""What a reader of the transcript sees for the builder's tools.

``session_record``: Read the record of <session> (or N sessions), each a session link
with its turns, tool calls, children and size, and the file it was written to.
``optimize_scope``: the window and the sessions in it, each linked with its evidence.
``optimize_report_shown``: whether the watermark moved and what was declined.
Refusals name their typed reason as a failure.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _row(result: Any, structured: Any) -> Mapping[str, Any]:
    for value in (structured, result):
        if isinstance(value, Mapping):
            return value
    return {}


def _failed(action: str, row: Mapping[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    reason = str(row.get("reason") or "failed")
    return {
        "action": action,
        "status": "failed",
        "summary": reason.replace("_", " ").capitalize(),
        "blocks": [
            *blocks,
            {
                "id": "refusal",
                "type": "text",
                "label": "Refused",
                "text": str(row.get("error") or reason),
                "severity": "error",
            },
        ],
    }


def _plural(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _session_link(sid: str, label: str, block_id: str) -> dict[str, Any]:
    return {"id": block_id, "type": "link", "target": "session", "uri": sid, "label": label or sid}


def session_record_start(args: Mapping[str, Any]) -> dict[str, Any]:
    wanted = [str(s) for s in args.get("sessions") or [] if str(s)]
    blocks = [_session_link(sid, sid, f"want-{i}") for i, sid in enumerate(wanted)]
    return {
        "action": "Read the record of",
        "subject": blocks[0]["id"] if len(blocks) == 1 else None,
        "status": "running",
        "summary": _plural(len(wanted), "session") if wanted else "this conversation",
        "blocks": blocks,
    }


def session_record(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    row = _row(result, structured)
    wanted = [str(s) for s in args.get("sessions") or [] if str(s)]
    if not row.get("written"):
        refused = [str(s) for s in row.get("refused") or wanted]
        blocks = [_session_link(sid, sid, f"refused-{i}") for i, sid in enumerate(refused)]
        return _failed("Read the record of", row, blocks)
    records = [r for r in row.get("records") or [] if isinstance(r, Mapping)]
    blocks: list[dict[str, Any]] = []
    for i, record in enumerate(records):
        sid = str(record.get("session_id") or "")
        blocks.append(_session_link(sid, str(record.get("title") or sid), f"session-{i}"))
        top = ", ".join(f"{t.get('tool')} {t.get('calls')}" for t in record.get("top_tools") or [])
        facts = [
            _plural(int(record.get("turns") or 0), "turn"),
            _plural(int(record.get("tool_calls") or 0), "tool call"),
            _plural(int(record.get("children") or 0), "child session"),
            f"{int(record.get('size_bytes') or 0) / 1024:.0f} KB",
        ]
        text = ", ".join(facts) + (f"\nMost used: {top}" if top else "")
        issues = [str(x) for x in record.get("issues") or [] if x]
        if issues:
            text += "\nGaps in the record: " + ", ".join(sorted(set(issues)))
        blocks.append({"id": f"facts-{i}", "type": "text", "text": text})
        blocks.append(
            {
                "id": f"file-{i}",
                "type": "link",
                "target": "file",
                "uri": str(record.get("path") or ""),
                "label": f"{sid}.json",
            }
        )
    title = str(records[0].get("title") or "") if len(records) == 1 else ""
    return {
        "action": "Read the record of",
        "subject": "session-0" if len(records) == 1 else None,
        "summary": title or _plural(len(records), "session"),
        "blocks": blocks,
    }


def optimize_scope(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    row = _row(result, structured)
    plugin = str(args.get("plugin") or "")
    action = (
        f"Find the sessions {plugin} ran in" if plugin else "Find the sessions to optimize from"
    )
    if not row.get("listed"):
        return _failed(action, row, [])
    sessions = [s for s in row.get("sessions") or [] if isinstance(s, Mapping)]
    window = {
        "named": "Sessions the person named",
        "since": f"Since {row.get('since')}",
        "days": f"Since {row.get('since')}",
        "plugin_window": f"Last 90 days, since {row.get('since')}",
        "watermark": f"Since the last /optimize, {row.get('since')}",
        "first_run": f"First run: the last 30 days, since {row.get('since')}",
    }.get(str(row.get("basis") or ""), "")
    blocks: list[dict[str, Any]] = [
        {"id": "window", "type": "text", "label": "Window", "text": window}
    ]
    for i, session in enumerate(sessions):
        sid = str(session.get("session_id") or "")
        blocks.append(_session_link(sid, str(session.get("title") or sid), f"session-{i}"))
        evidence = [str(e) for e in session.get("evidence") or []]
        detail = f"{session.get('updated_at', '')}, {_plural(int(session.get('message_count') or 0), 'message')}"
        if evidence:
            detail += "\n" + "; ".join(evidence)
        blocks.append({"id": f"session-{i}-detail", "type": "text", "text": detail})
    if row.get("left_out"):
        blocks.append(
            {
                "id": "left-out",
                "type": "text",
                "label": "Not listed",
                "text": f"{row['left_out']} older sessions are past the scope's limit.",
                "severity": "warning",
            }
        )
    declined = [d for d in row.get("declined") or [] if isinstance(d, Mapping)]
    if declined:
        blocks.append(
            {
                "id": "declined",
                "type": "text",
                "label": "Declined before",
                "text": "\n".join(str(d.get("summary") or d.get("key")) for d in declined),
            }
        )
    return {
        "action": action,
        "summary": _plural(len(sessions), "session") if sessions else "No sessions in scope",
        "blocks": blocks,
    }


def optimize_report_shown(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    row = _row(result, structured)
    action = "Record the optimize report"
    if not row.get("recorded"):
        return _failed(action, row, [])
    blocks: list[dict[str, Any]] = []
    if row.get("watermark_moved"):
        blocks.append(
            {
                "id": "watermark",
                "type": "text",
                "label": "Next /optimize starts",
                "text": str(row.get("watermark") or ""),
            }
        )
    declined = [d for d in args.get("declined") or [] if isinstance(d, Mapping)]
    if declined:
        blocks.append(
            {
                "id": "declined",
                "type": "text",
                "label": "Declined",
                "text": "\n".join(str(d.get("summary") or d.get("key")) for d in declined),
            }
        )
    summary = "Watermark moved" if row.get("watermark_moved") else "Recorded"
    if declined:
        summary += f", {_plural(len(declined), 'finding')} declined"
    return {"action": action, "summary": summary, "blocks": blocks}
