"""``session_record``: the full record of named sessions, as files the agent reads in code.

Each selected session's raw ``clio.session-export.v2`` record (the export route's
server-side assembly, :func:`clio_agent.gact.session_export.build_transcript`:
messages, full semantic events, tool records, loaded skills and every descendant
session) is written to ``<scratch>/session-records/<session>.json``. Beside it,
``<session>.agent.json`` names the plugins its agent resolved, with what each brings,
so a trigger miss (a plugin that was there and fit, but was not used) can be seen.

Only the selected sessions' records are written: no workspace contents and no
credentials, as in the export. The record is a file, not a context read: a long
session costs nothing until the agent queries it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from clio_agent.gact.builder.scope import check_named

RECORDS_DIR = "session-records"


def _agent_summary(app: Any, sid: str) -> dict[str, Any]:
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    try:
        resolution = session_resolution(app, sid)
    except (SessionAgentUnavailable, LookupError, ValueError, OSError) as exc:
        return {"session_id": sid, "unavailable": f"{type(exc).__name__}: {exc}"}
    skills: dict[str, list[str]] = {}
    for row in resolution.skills:
        skills.setdefault(str(row.get("plugin") or ""), []).append(str(row.get("id") or ""))
    tools: dict[str, list[str]] = {}
    for row in resolution.tools:
        tools.setdefault(str(row.get("plugin") or ""), []).append(str(row.get("name") or ""))
    return {
        "session_id": sid,
        "plugins": [
            {
                "id": row.get("id"),
                "version": row.get("version"),
                "marketplace": row.get("marketplace"),
                "skills": sorted(skills.get(str(row.get("id")), [])),
                "tools": sorted(tools.get(str(row.get("id")), [])),
            }
            for row in resolution.plugins
        ],
    }


def _summary(record: dict[str, Any], path: Path, size: int) -> dict[str, Any]:
    def tools_of(transcript: dict[str, Any]) -> Counter[str]:
        return Counter(str(row.get("tool") or "") for row in transcript.get("tool_records") or [])

    counts = tools_of(record)
    for child in record.get("children") or []:
        counts += tools_of(child)
    messages = record.get("messages") or []
    session = record.get("session") or {}
    return {
        "session_id": str(session.get("id") or ""),
        "title": str(session.get("title") or ""),
        "path": str(path),
        "agent_path": str(path.with_suffix(".agent.json")),
        "size_bytes": size,
        "turns": sum(1 for m in messages if m.get("role") == "user"),
        "messages": len(messages),
        "tool_calls": sum(counts.values()),
        "top_tools": [{"tool": name, "calls": n} for name, n in counts.most_common(8) if name],
        "children": len(record.get("children") or []),
        "issues": [row.get("reason") for row in (record.get("recording") or {}).get("issues", [])],
    }


def write_records(app: Any, session_id: str, sessions: list[str]) -> dict[str, Any]:
    """Write the named sessions' records into ``session_id``'s scratch folder."""

    from clio_agent.gact.session_export import build_transcript  # noqa: PLC0415
    from clio_agent.runtime.session_scratch import session_scratch_dir  # noqa: PLC0415

    targets = check_named(app, session_id, sessions)
    scratch = session_scratch_dir(session_id)
    if scratch is None:
        return {
            "written": False,
            "reason": "scratch_unavailable",
            "error": "this conversation has no scratch folder to write the records into",
        }
    folder = scratch / RECORDS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for sid in targets:
        record = build_transcript(app, sid)
        path = folder / f"{sid}.json"
        text = json.dumps(record, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        path.with_suffix(".agent.json").write_text(
            json.dumps(_agent_summary(app, sid), indent=2), encoding="utf-8"
        )
        rows.append(_summary(record, path, len(text.encode("utf-8"))))
    return {"written": True, "folder": str(folder), "records": rows}
