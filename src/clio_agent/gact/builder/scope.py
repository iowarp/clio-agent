"""Which sessions the builder may read, and the scope ``/optimize`` reads.

A session's record is read only when the person selected it:

* the conversation itself, and the sessions its person referenced with ``@`` or
  wrote by id in their own messages, in this conversation or any conversation above
  it (a scan child reads what its parent's person named);
* the scope ``optimize_scope`` showed in this conversation (:data:`SCOPE_KEY`), which
  the agent shows the person before it reads anything.

Anything else is refused with ``session_not_named``.

``optimize_scope`` lists the root conversations in scope with what each shows of the
plugin: for ``/optimize <plugin>``, the sessions where one of its tools or flows was
called, one of its skills loaded, one of its subagents started, or its hooks were
active; for the global ``/optimize``, every root conversation since the account's
watermark (the last 30 days on the first run). The watermark and the findings the
person declined live in ``<plugin store>/builder/optimize.json``; the watermark moves
only when the report is shown (``optimize_report_shown``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: Session metadata: the scope ``optimize_scope`` last showed in this conversation.
SCOPE_KEY = "builder_scope"
#: The kinds of session a person starts (children and side lookups are read with
#: their root, through its record).
ROOT_KINDS = frozenset({"conversation", "branch"})
#: The global scope's window on its first run.
FIRST_RUN_DAYS = 30
#: How far back ``/optimize <plugin>`` looks by default.
PLUGIN_WINDOW_DAYS = 90
#: The most sessions one scope lists; more is reported, never silently cut.
SCOPE_LIMIT = 200
_SESSION_ID = re.compile(r"\bsess_[A-Za-z0-9_-]{6,}\b")


class ScopeRefused(ValueError):
    """A typed refusal: ``reason`` is the code the tool returns."""

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------------------ named sessions


def _person_named(app: Any, session_id: str) -> set[str]:
    named: set[str] = set()
    for message in app.state.messages.get(session_id, []):
        if getattr(message, "role", "") != "user":
            continue
        for part in getattr(message, "parts", []) or []:
            if getattr(part, "ref_kind", "") == "session" and getattr(part, "ref_id", ""):
                named.add(str(part.ref_id))
            text = str(getattr(part, "text", "") or "")
            named.update(_SESSION_ID.findall(text))
    return named


def _lineage(app: Any, session_id: str) -> list[str]:
    chain: list[str] = []
    current = session_id
    while current and current not in chain and len(chain) < 16:
        chain.append(current)
        session = app.state.sessions.get(current)
        current = str(getattr(session, "parent_session_id", "") or "") if session else ""
    return chain


def named_sessions(app: Any, session_id: str) -> set[str]:
    """Every session this conversation (or one above it) selected for reading."""

    named: set[str] = set()
    for sid in _lineage(app, session_id):
        named.add(sid)
        named |= _person_named(app, sid)
        session = app.state.sessions.get(sid)
        metadata = getattr(session, "metadata", None) or {}
        scope = metadata.get(SCOPE_KEY) if isinstance(metadata, Mapping) else None
        if isinstance(scope, Mapping):
            named.update(str(item) for item in scope.get("sessions") or [])
    return named


def check_named(app: Any, session_id: str, wanted: Iterable[str]) -> list[str]:
    """``wanted`` (default: this conversation), refused unless every one was named."""

    targets = list(dict.fromkeys(str(s).strip() for s in wanted if str(s).strip()))
    if not targets:
        return [session_id]
    allowed = named_sessions(app, session_id)
    refused = [sid for sid in targets if sid not in allowed]
    if refused:
        raise ScopeRefused(
            "session_not_named",
            "only this conversation, the sessions the person referenced with @ or by id, "
            "and the scope optimize_scope showed can be read; not named: " + ", ".join(refused),
            refused=refused,
        )
    missing = [sid for sid in targets if app.state.sessions.get(sid) is None]
    if missing:
        raise ScopeRefused(
            "session_not_found", "no such session: " + ", ".join(missing), refused=missing
        )
    return targets


# ------------------------------------------------------------------ plugin usage


def plugin_parts(app: Any, plugin: str) -> dict[str, Any]:
    """What ``plugin`` brings, from the installed records: what counts as it running."""

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    records = [r for r in plugin_service(app).store.records() if r.id == plugin]
    if not records:
        raise ScopeRefused(
            "plugin_not_installed", f"no installed plugin is named {plugin!r}", plugin=plugin
        )
    record = records[-1]
    return {
        "id": record.id,
        "version": record.version,
        "marketplace": record.marketplace,
        "tools": sorted({t.name for t in record.tools}),
        "servers": sorted(record.mcp_servers),
        "skills": sorted({s.id for s in record.skills}),
        "flows": sorted({f.id for f in record.flows}),
        "subagents": sorted({s.id for s in record.subagents}),
        "hooks": sorted({h.id for h in record.hooks}),
    }


def _resolved_ids(app: Any, session: Any) -> set[str] | str:
    """The plugin ids ``session`` resolved, or the reason they cannot be read."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_REF_KEY,
        AGENT_RESOLUTION_KEY,
        SessionAgentUnavailable,
        pinned_resolution,
    )

    metadata = getattr(session, "metadata", None) or {}
    digest = str(metadata.get(AGENT_RESOLUTION_KEY) or "")
    if not digest:
        return "it pinned no agent"
    try:
        resolution = pinned_resolution(app, digest, str(metadata.get(AGENT_REF_KEY) or ""))
    except (SessionAgentUnavailable, LookupError, ValueError, OSError) as exc:
        return f"its agent cannot be read ({type(exc).__name__}: {exc})"
    return {str(row.get("id")) for row in resolution.plugins}


def plugin_evidence(app: Any, session: Any, parts: Mapping[str, Any]) -> list[str]:
    """How ``parts``' plugin ran in ``session`` (empty: it did not)."""

    tools, flows = set(parts["tools"]), set(parts["flows"])
    skills, subagents = set(parts["skills"]), set(parts["subagents"])
    prefixes = tuple(f"{server}_" for server in parts["servers"])
    seen: dict[str, int] = {}
    for message in app.state.messages.get(session.id, []):
        for part in getattr(message, "parts", []) or []:
            if getattr(part, "type", "") != "tool_call":
                continue
            name = str(getattr(part, "tool_name", "") or "")
            args = getattr(part, "input", None) or {}
            if name in tools or (prefixes and name.startswith(prefixes)):
                key = f"called {name}"
            elif name == "load_skill" and str(args.get("skill_id") or "") in skills:
                key = f"loaded skill {args['skill_id']}"
            elif name in {"run_flow", "run_workflow"} and str(args.get("flow") or "") in flows:
                key = f"ran flow {args['flow']}"
            elif str(args.get("agent") or "") in subagents and "spawn" in name:
                key = f"started {args['agent']}"
            else:
                continue
            seen[key] = seen.get(key, 0) + 1
    evidence = [f"{key} ({count}x)" if count > 1 else key for key, count in seen.items()]
    if parts["hooks"]:
        resolved = _resolved_ids(app, session)
        if isinstance(resolved, str):
            # Not silently "no hooks": the scope lists the session and says why.
            evidence.append(f"hooks unknown: {resolved}")
        elif parts["id"] in resolved:
            evidence.append("its hooks were active")
    return evidence


# ------------------------------------------------------------------ the account's state


def _state_path(app: Any) -> Path:
    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    return Path(plugin_service(app).store.root) / "builder" / "optimize.json"


def read_state(app: Any) -> dict[str, Any]:
    path = _state_path(app)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"watermark": "", "declined": []}
    if not isinstance(state, dict):
        raise ScopeRefused("optimize_state_unreadable", f"{path} does not hold an object")
    return {"watermark": str(state.get("watermark") or ""), "declined": state.get("declined") or []}


def write_state(app: Any, state: Mapping[str, Any]) -> None:
    path = _state_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".writing")
    staging.write_text(json.dumps(dict(state), indent=2), encoding="utf-8")
    staging.replace(path)


# ------------------------------------------------------------------ the scope


def _root_sessions(app: Any) -> list[Any]:
    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    query = PageQuery(archived=None, kinds=ROOT_KINDS, limit=100_000)
    return [s for s in app.state.sessions.select(query) if s is not None]


def _row(session: Any, evidence: list[str]) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "title": str(getattr(session, "title", "") or ""),
        "created_at": str(getattr(session, "created_at", "") or ""),
        "updated_at": str(getattr(session, "updated_at", "") or ""),
        "message_count": int(getattr(session, "message_count", 0) or 0),
        "evidence": evidence,
    }


def build_scope(
    app: Any,
    session_id: str,
    *,
    plugin: str = "",
    sessions: Iterable[str] = (),
    since: str = "",
    days: int = 0,
) -> dict[str, Any]:
    """The sessions ``/optimize`` would read, recorded on this conversation as shown."""

    now = _now()
    state = read_state(app)
    named = [s for s in sessions if str(s).strip()]
    parts = plugin_parts(app, plugin) if plugin else None
    if named:
        rows = [app.state.sessions.get(sid) for sid in check_named(app, session_id, named)]
        start, basis = None, "named"
    else:
        if since:
            start = _parse_time(since)
            if start is None:
                raise ScopeRefused("since_invalid", f"{since!r} is not an ISO date or time")
            basis = "since"
        elif days > 0:
            start, basis = now - timedelta(days=days), "days"
        elif parts is not None:
            start, basis = now - timedelta(days=PLUGIN_WINDOW_DAYS), "plugin_window"
        elif state["watermark"]:
            start, basis = _parse_time(state["watermark"]), "watermark"
        else:
            start, basis = now - timedelta(days=FIRST_RUN_DAYS), "first_run"
        rows = [
            s
            for s in _root_sessions(app)
            if s.id != session_id and (start is None or (_parse_time(s.updated_at) or now) > start)
        ]
    listed: list[dict[str, Any]] = []
    for session in sorted(rows, key=lambda s: str(s.updated_at)):
        evidence = plugin_evidence(app, session, parts) if parts else []
        if parts is not None and not named and not evidence:
            continue
        listed.append(_row(session, evidence))
    truncated = max(0, len(listed) - SCOPE_LIMIT)
    listed = listed[-SCOPE_LIMIT:] if truncated else listed
    scope = {
        "plugin": parts,
        "basis": basis,
        "since": start.isoformat() if start else "",
        "until": now.isoformat(),
        "sessions": listed,
        "count": len(listed),
        "left_out": truncated,
        "watermark": state["watermark"],
        "declined": state["declined"],
    }
    _remember_scope(app, session_id, scope)
    return scope


def _remember_scope(app: Any, session_id: str, scope: Mapping[str, Any]) -> None:
    shown = {
        "sessions": [row["session_id"] for row in scope["sessions"]],
        "until": scope["until"],
        "global": scope["plugin"] is None,
        "shown_at": _now().isoformat(),
    }
    app.state.sessions.update(session_id, metadata_patch={SCOPE_KEY: shown})


def report_shown(
    app: Any, session_id: str, declined: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Move the watermark to the shown global scope's end and keep the declined findings."""

    session = app.state.sessions.get(session_id)
    metadata = getattr(session, "metadata", None) or {}
    scope = metadata.get(SCOPE_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(scope, Mapping):
        raise ScopeRefused(
            "scope_not_shown", "optimize_scope has not shown a scope in this conversation"
        )
    state = read_state(app)
    rows = [
        {
            "key": str(row.get("key") or row.get("summary") or "").strip(),
            "plugin": str(row.get("plugin") or ""),
            "summary": str(row.get("summary") or ""),
            "sessions": [str(s) for s in row.get("sessions") or []],
            "declined_at": _now().isoformat(),
        }
        for row in declined
        if isinstance(row, Mapping) and (row.get("key") or row.get("summary"))
    ]
    known = {row.get("key") for row in state["declined"]}
    state["declined"] = [*state["declined"], *(r for r in rows if r["key"] not in known)]
    moved = bool(scope.get("global"))
    previous = state["watermark"]
    if moved:
        state["watermark"] = str(scope.get("until") or "")
    write_state(app, state)
    return {
        "recorded": True,
        "watermark_moved": moved,
        "watermark": state["watermark"],
        "previous_watermark": previous,
        "declined_recorded": len(rows),
        "declined_total": len(state["declined"]),
        "sessions": len(scope.get("sessions") or []),
    }
