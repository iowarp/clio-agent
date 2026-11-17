"""The "Report a problem" diagnostic report (owner module).

One report for a person who cannot open a terminal: what this CLIO is and where it
runs, the health summary ``/v1/health`` already serves, the tail of the same log
files ``clio logs`` shows, and optionally the recent reply outcomes of one
conversation the person picked. Message text is included only when the person
asks for it.

Every string in the report passes through one server-side
:class:`~clio_agent.runtime.report_redaction.Redactor` before it leaves this
process; the plain-text rendering (Copy / Download) is built from the redacted
report, so both carry exactly the same content. Missing pieces are typed reasons
(``degradation``), never empty sections.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from clio_agent import paths
from clio_agent.gact.runtime.constants import CONTRACT_VERSION, GACT_BACKEND_VERSION
from clio_agent.runtime import log_sources
from clio_agent.runtime.report_redaction import Redactor, redactor_for_this_computer

if TYPE_CHECKING:
    from fastapi import FastAPI

SCHEMA = "clio.problem_report.v1"
ClientKind = Literal["desktop", "web"]
#: Replies of the picked conversation the report describes, newest last.
RECENT_REPLIES = 5
#: Messages included when the person ticks "include this conversation's messages".
INCLUDED_MESSAGES = 20
_MESSAGE_CHARS = 4000


def _degradation(reason: str, message: str) -> dict[str, str]:
    return {"reason": reason, "message": message}


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


# ---- About ------------------------------------------------------------------------


def _os_description() -> str:
    system = platform.system()
    if system == "Darwin":
        release = platform.mac_ver()[0]
        return f"macOS {release}".strip()
    if system == "Windows":
        return f"Windows {platform.release()} ({platform.version()})"
    return f"{system} {platform.release()}".strip()


def _about(app: FastAPI, client: ClientKind | None, client_version: str | None) -> dict[str, Any]:
    from clio_agent.arc.home_runtime import desktop_managed  # noqa: PLC0415 - leaf, lazy
    from clio_agent.gact.providers.config import _effective_lm_config  # noqa: PLC0415

    lm = _effective_lm_config(app)
    managed = desktop_managed()
    app_kind: ClientKind | Literal["unknown"]
    if client is not None:
        app_kind, app_kind_source = client, "reported by the app"
    elif managed:
        app_kind, app_kind_source = "desktop", "started by the desktop app"
    else:
        app_kind, app_kind_source = "unknown", "not reported"
    return {
        "version": GACT_BACKEND_VERSION,
        "protocol": CONTRACT_VERSION,
        "os": _os_description(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "app_kind": app_kind,
        "app_kind_source": app_kind_source,
        "app_version": client_version or os.environ.get("GACT_DESKTOP_VERSION") or None,
        "desktop_managed": managed,
        "provider": str(lm.get("provider_id") or lm.get("provider") or "") or None,
        "model": str(lm.get("model") or "") or None,
    }


# ---- Health -----------------------------------------------------------------------


def _health_endpoint(app: FastAPI) -> Any:
    for route in app.router.routes:
        if getattr(route, "path", None) == "/v1/health" and "GET" in (
            getattr(route, "methods", None) or ()
        ):
            return getattr(route, "endpoint", None)
    return None


async def _health(app: FastAPI) -> dict[str, Any]:
    """The ``/v1/health`` answer itself, summarised: overall state and rows not ready."""

    endpoint = _health_endpoint(app)
    if endpoint is None:
        return {
            "overall_status": None,
            "rows": [],
            "degradation": _degradation(
                "health_route_absent", "This service does not serve a health summary."
            ),
        }
    try:
        result = await endpoint()
    except Exception as exc:  # noqa: BLE001 - forwards: the report's typed health reason
        return {
            "overall_status": None,
            "rows": [],
            "degradation": _degradation(
                "health_collection_failed", f"The health check failed: {exc!r}"
            ),
        }
    if hasattr(result, "model_dump"):
        body = result.model_dump(mode="json", exclude_none=True)
    else:  # the 503 path answers with a JSONResponse
        body = json.loads(bytes(getattr(result, "body", b"{}")) or b"{}")
    rows = [
        {
            "name": str(row.get("name") or ""),
            "status": str(row.get("status") or ""),
            "summary": str(row.get("summary") or row.get("detail") or ""),
            "reason": row.get("reason"),
            "next_action": row.get("next_action"),
            "required": bool(row.get("required", False)),
        }
        for row in body.get("integrations") or []
    ]
    return {
        "overall_status": body.get("overall_status"),
        "uptime_s": body.get("uptime_s"),
        "ready_count": sum(1 for row in rows if row["status"] == "ready"),
        "rows": rows,
        "degradation": None,
    }


# ---- Logs -------------------------------------------------------------------------


def _logs(lines: int) -> dict[str, Any]:
    count = log_sources.clamp_lines(lines)
    tails = log_sources.collect_log_tails(count)
    sources = [tail for tail in tails if tail.status != "missing"]
    searched = [tail for tail in tails if tail.status == "missing"]
    degradation = None
    if not any(tail.lines for tail in sources):
        degradation = _degradation(
            "no_log_files",
            "No CLIO log file was found on this computer. A service started from a "
            "terminal writes its log to that terminal instead of a file.",
        )
    return {
        "lines_requested": count,
        "max_lines": log_sources.MAX_TAIL_LINES,
        "sources": [
            {
                "role": tail.role,
                "path": str(tail.path),
                "origin": tail.origin,
                "status": tail.status,
                "reason": tail.reason or None,
                "message": tail.message or None,
                "size_bytes": tail.size_bytes,
                "modified_at": _iso(tail.modified_at),
                "earlier_lines": tail.earlier_lines,
                "lines": tail.lines,
            }
            for tail in sources
        ],
        "searched": [
            {"role": tail.role, "path": str(tail.path), "reason": tail.reason} for tail in searched
        ],
        "degradation": degradation,
    }


# ---- Conversation -----------------------------------------------------------------


def _part_text(part: Any) -> str:
    return str(getattr(part, "text", "") or "")


def _reply_outcome(message: Any) -> dict[str, Any]:
    error = getattr(message, "error_info", None)
    error_parts = [part for part in getattr(message, "parts", []) if part.type == "error"]
    code = getattr(error, "error", None)
    text = getattr(error, "message", None)
    if code is None and error_parts:
        part = error_parts[-1]
        code = str((part.metadata or {}).get("code") or "error")
        text = _part_text(part)
    has_content = any(
        part.type not in {"error", "step-start", "step-finish"} for part in message.parts
    )
    outcome = "failed" if code else "completed" if has_content else "empty"
    return {
        "message_id": message.id,
        "turn_id": message.turn_id or None,
        "created_at": message.created_at,
        "outcome": outcome,
        "stop_reason": message.stop_reason or None,
        "error_code": code,
        "error_message": text,
        "recoverable": bool(getattr(error, "recoverable", False)) if error else None,
        "model": "/".join(
            value
            for value in (
                str((message.metadata or {}).get("provider_id") or ""),
                str((message.metadata or {}).get("model_id") or ""),
            )
            if value
        )
        or None,
    }


def _model_label(model: Any) -> str | None:
    """``provider/model`` from a session's model reference (a model or a plain mapping)."""

    if model is None:
        return None
    if not isinstance(model, dict):
        model = {"provider_id": model.provider_id, "model_id": model.model_id}
    parts = (str(model.get("provider_id") or ""), str(model.get("model_id") or ""))
    return "/".join(part for part in parts if part) or None


def _conversation(app: FastAPI, session_id: str, include_messages: bool) -> dict[str, Any]:
    session = app.state.sessions.get(session_id)
    if session is None:
        return {
            "id": session_id,
            "messages_included": False,
            "degradation": _degradation(
                "conversation_not_found", "This conversation is not on this service."
            ),
        }
    messages = list(app.state.messages.get(session_id, []))
    runner = getattr(app.state, "turn_runner", None)
    replies = [message for message in messages if message.role == "assistant"]
    section: dict[str, Any] = {
        "id": session.id,
        # A title is written from the person's own words, so it travels with them.
        "title": session.title if include_messages else None,
        "status": session.status,
        "busy": bool(runner.busy(session.id)) if runner is not None else None,
        "model": _model_label(getattr(session, "model", None)),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "message_count": len(messages),
        "recent_replies": [_reply_outcome(message) for message in replies[-RECENT_REPLIES:]],
        "messages_included": include_messages,
        "messages": None,
        "degradation": None,
    }
    if include_messages:
        section["messages"] = [
            {
                "role": message.role,
                "created_at": message.created_at,
                "text": "\n".join(
                    _part_text(part) for part in message.parts if part.type == "text"
                )[:_MESSAGE_CHARS],
            }
            for message in messages[-INCLUDED_MESSAGES:]
        ]
    return section


# ---- Redaction and text -----------------------------------------------------------


def _redact(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.apply(value)
    if isinstance(value, list):
        return [_redact(item, redactor) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, redactor) for key, item in value.items()}
    return value


def _manifest(report: dict[str, Any]) -> list[dict[str, str]]:
    logs = report["logs"]
    entries = [
        {"section": "about", "title": "About", "contents": "Version, system, app and model."},
        {
            "section": "health",
            "title": "Health",
            "contents": "Overall status and every health check with its summary.",
        },
    ]
    for source in logs["sources"]:
        entries.append(
            {
                "section": "log",
                "title": f"Log: {source['role']}",
                "contents": f"Last {len(source['lines'])} lines of {source['path']}.",
            }
        )
    if logs["degradation"]:
        entries.append({"section": "log", "title": "Log", "contents": "No log file found."})
    conversation = report.get("conversation")
    if conversation:
        entries.append(
            {
                "section": "conversation",
                "title": "Conversation",
                "contents": (
                    "Recent reply outcomes and the last messages' text."
                    if conversation.get("messages_included")
                    else "Recent reply outcomes and errors; no message text."
                ),
            }
        )
    entries.append(
        {
            "section": "redaction",
            "title": "Secrets removed",
            "contents": "What was removed before the report was made, by kind.",
        }
    )
    return entries


def render_text(report: dict[str, Any]) -> str:
    """The report as one plain-text document: manifest first, then each section."""

    about, health, logs = report["about"], report["health"], report["logs"]
    out: list[str] = [
        "CLIO problem report",
        f"Generated: {report['generated_at']}",
        "Secrets (keys, tokens, passwords, cookies) were removed by the CLIO service.",
        "",
        "== Contents ==",
    ]
    out += [f"- {entry['title']}: {entry['contents']}" for entry in report["manifest"]]
    out += ["", "== About =="]
    for label, key in (
        ("CLIO version", "version"),
        ("Operating system", "os"),
        ("Platform", "platform"),
        ("App", "app_kind"),
        ("App version", "app_version"),
        ("Provider", "provider"),
        ("Model", "model"),
        ("Python", "python"),
    ):
        out.append(f"{label}: {about.get(key) or 'not reported'}")
    out += ["", "== Health =="]
    if health.get("degradation"):
        deg = health["degradation"]
        out.append(f"Unavailable: {deg['message']} ({deg['reason']})")
    else:
        out.append(f"Overall: {health.get('overall_status')}")
        for row in health["rows"]:
            reason = f" [{row['reason']}]" if row.get("reason") else ""
            out.append(f"- {row['name']}: {row['status']}{reason}. {row['summary']}".rstrip())
    conversation = report.get("conversation")
    if conversation:
        out += ["", "== Conversation =="]
        if conversation.get("degradation"):
            deg = conversation["degradation"]
            out.append(f"{conversation['id']}: {deg['message']} ({deg['reason']})")
        else:
            out.append(f"Conversation: {conversation['id']}")
            if conversation.get("title"):
                out.append(f"Title: {conversation['title']}")
            out.append(
                f"State: {conversation['status']}"
                f"{', a reply is running' if conversation.get('busy') else ''}"
            )
            out.append(f"Model: {conversation.get('model') or 'service default'}")
            out.append(f"Messages: {conversation['message_count']}")
            for reply in conversation["recent_replies"]:
                line = f"- {reply['created_at']} reply {reply['outcome']}"
                if reply.get("error_code"):
                    line += f": {reply['error_code']}"
                if reply.get("error_message"):
                    line += f" ({reply['error_message']})"
                out.append(line)
            if conversation.get("messages") is not None:
                out += ["", "-- Messages (included at the person's request) --"]
                for message in conversation["messages"]:
                    out.append(f"[{message['created_at']}] {message['role']}:")
                    out.append(message["text"])
    out += ["", "== Logs =="]
    if logs.get("degradation"):
        out.append(f"{logs['degradation']['message']} ({logs['degradation']['reason']})")
        out += [f"Looked in: {item['path']}" for item in logs["searched"]]
    for source in logs["sources"]:
        out += ["", f"-- {source['role']} log: {source['path']} --"]
        if source.get("message"):
            out.append(f"({source['message']})")
        if source.get("earlier_lines"):
            out.append(f"(last {len(source['lines'])} lines; earlier lines not included)")
        out += source["lines"]
    out += ["", "== Secrets removed =="]
    out += [f"- {item['description']}: {item['count']}" for item in report["redaction"]]
    return "\n".join(out) + "\n"


def report_filename(generated_at: datetime) -> str:
    return f"clio-problem-report-{generated_at.strftime('%Y%m%d-%H%M%S')}.txt"


async def build_problem_report(
    app: FastAPI,
    *,
    lines: int | None = None,
    session_id: str | None = None,
    include_messages: bool = False,
    client: ClientKind | None = None,
    client_version: str | None = None,
    redactor: Redactor | None = None,
) -> dict[str, Any]:
    """Collect, redact and render the report. Every section is present or has a reason."""

    from clio_agent.gact.auth import configured_bearer_token  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    if redactor is None:
        try:
            config_dir = paths.user_config_dir()
        except paths.HomeDirectoryUnavailable:
            config_dir = None
        token = configured_bearer_token()
        redactor = await asyncio.to_thread(
            redactor_for_this_computer, config_dir, [token] if token else []
        )
    raw: dict[str, Any] = {
        "about": _about(app, client, client_version),
        "health": await _health(app),
        "logs": await asyncio.to_thread(_logs, lines or log_sources.DEFAULT_TAIL_LINES),
        "conversation": (_conversation(app, session_id, include_messages) if session_id else None),
    }
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "filename": report_filename(now),
        **_redact(raw, redactor),
    }
    report["redaction"] = redactor.summary()
    report["manifest"] = _manifest(report)
    report["text"] = render_text(report)
    return report


__all__ = [
    "INCLUDED_MESSAGES",
    "RECENT_REPLIES",
    "SCHEMA",
    "build_problem_report",
    "render_text",
    "report_filename",
]
