"""GACT v0.2 FastAPI application for CLIO.

Exposes the GACT v0.2 contract surface. Most routes are 501 stubs
today (CLIO-BBBBBBBBBB6); they get wired one at a time in
follow-on iterations (BBB7–BBB12) against the spec at
``gact-tui/contract/SPEC.md`` and the docs in ``docs/tui/``.

Run via::

    clio-agent-gact --host 127.0.0.1 --port 8100

Or::

    uvicorn clio_agent.gact.app:app --host 127.0.0.1 --port 8100

This is a peer of ``clio_agent.ui.api`` (the native CLIO REST API),
not a replacement — both can run side-by-side. The TUI integration
target is the GACT app; existing CLI + direct-Python callers keep
using the native API unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import fnmatch
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from clio_agent.gact.workspace_scope import (
    GLOBAL_WORKSPACE_ID,
    resolve_workspace_storage_root,
    session_scope_label,
    workspace_scope,
)
from clio_agent.prompts import PromptRegistry, PromptSource
from clio_agent.tools.file_policy import validate_write_path
from clio_agent.tools.fs_write import write_text_with_policy

_ACTIVE_TOOL_SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "clio_gact_active_tool_session_id",
    default="",
)


@contextmanager
def _tool_session_context(sid: str) -> Iterator[None]:
    """Bind GACT tool hooks to the session driving the current turn."""
    token = _ACTIVE_TOOL_SESSION_ID.set(sid)
    try:
        yield
    finally:
        _ACTIVE_TOOL_SESSION_ID.reset(token)


def _resolve_tool_session(app: "FastAPI") -> tuple[str, Any | None]:
    """Return the active turn session, falling back to recency for out-of-band calls."""
    sid = _ACTIVE_TOOL_SESSION_ID.get().strip()
    if sid:
        return sid, app.state.sessions.get(sid)
    sessions_by_recency = app.state.sessions.list()
    if sessions_by_recency:
        current = sessions_by_recency[0]
        return current.id, current
    return "", None


def _format_sse(event: "Event") -> bytes:
    """Render an Event as the SSE wire format (SPEC §7.2)::

    event: <type>
    id: <numeric monotonic id>
    data: <json envelope>
    <blank line>
    """

    payload = json.dumps(event.envelope())
    lines = f"event: {event.type}\nid: {event.id}\ndata: {payload}\n\n"
    return lines.encode("utf-8")


# ---- ID + timestamp helpers used by the message endpoint ---------
# Kept at module level (not inside build_app) so they're trivially
# importable by future streaming code + easy to mock in tests.


def _new_message_id(role_prefix: str) -> str:
    """Generate a message id. Role prefix ('user' / 'asst' / 'tool')
    makes log scraping + human triage cheaper."""

    return f"msg_{role_prefix}_{uuid.uuid4().hex[:12]}"


def _new_part_id() -> str:
    return f"part_{uuid.uuid4().hex[:12]}"


def _new_cancellation_attempt_id() -> str:
    return f"cancel_{uuid.uuid4().hex[:12]}"


def _new_question_id() -> str:
    return f"ques_{uuid.uuid4().hex[:12]}"


def _new_attempt_id() -> str:
    return f"att_{uuid.uuid4().hex[:12]}"


def _new_context_frame_id() -> str:
    return f"ctx_{uuid.uuid4().hex[:12]}"


def _new_memory_event_id() -> str:
    return f"mem_{uuid.uuid4().hex[:12]}"


def _iso_from_epoch(ts: float) -> str:
    """ISO-8601 UTC with microsecond precision to match the session
    registry's created_at format."""

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _expert_handoff_summary(handoff: Mapping[str, Any]) -> str:
    """Return a compact user-facing summary for an expert handoff part."""

    agent = str(handoff.get("agent_id") or handoff.get("expert") or "expert")
    parent = str(handoff.get("parent_id") or handoff.get("parent") or "").strip()
    status = str(handoff.get("status") or "observed")
    stage = str(handoff.get("stage") or handoff.get("dispatch_target") or "").strip()
    output = str(handoff.get("output_summary") or handoff.get("summary") or "").strip()
    route = f"{parent} -> {agent}" if parent else agent
    bits = [route, status]
    if stage:
        bits.append(stage)
    if output:
        bits.append(output)
    return " | ".join(bits)


def _format_subagent_input(spawn_input: Any) -> str:
    """Format a materialized nanoagent input without a raw Python-dict look."""

    if isinstance(spawn_input, str):
        return spawn_input
    try:
        return "Subagent input:\n" + json.dumps(spawn_input, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return f"Subagent input:\n{spawn_input}"


def _compact_exact_evidence_index(transcript: str) -> str:
    """Build a deterministic evidence index to append to LM compact summaries."""
    paths: list[str] = []
    identifiers: list[str] = []
    caveats: list[str] = []

    def add_unique(target: list[str], value: str, *, limit: int) -> None:
        cleaned = " ".join(value.strip("`'\" \t\r\n,.;:()[]{}").split())
        cleaned = cleaned.rstrip("/")
        if not cleaned or cleaned in target:
            return
        if len(cleaned) > 180:
            cleaned = cleaned[:177] + "..."
        if len(target) < limit:
            target.append(cleaned)

    quoted = re.findall(r"`([^`]+)`", transcript)
    for item in quoted:
        if re.search(r"\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar)\b", item, re.I):
            add_unique(paths, item, limit=40)
        elif re.search(r"[/_]", item) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,}", item):
            add_unique(identifiers, item, limit=80)

    path_pattern = re.compile(
        r"(?:[A-Za-z]:\\[^\r\n`\"<>|]*?\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar))"
        r"|(?:/[^\s`\"<>|]*?\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar))",
        re.I,
    )
    for match in path_pattern.finditer(transcript):
        add_unique(paths, match.group(0), limit=40)

    identifier_pattern = re.compile(
        r"(?<![A-Za-z0-9])/?[A-Za-z][A-Za-z0-9]*(?:[_/.-][A-Za-z0-9]+)+\b",
    )
    for match in identifier_pattern.finditer(transcript):
        value = match.group(0)
        if len(value) < 4:
            continue
        if value.lower().startswith(("http", "https")):
            continue
        add_unique(identifiers, value, limit=80)

    caveat_terms = (
        "error",
        "failed",
        "missing",
        "unavailable",
        "not installed",
        "caveat",
        "unresolved",
        "follow-up",
        "follow up",
        "needs checking",
        "action needed",
    )
    for raw_line in transcript.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        lowered = line.lower()
        if any(term in lowered for term in caveat_terms):
            add_unique(caveats, line, limit=16)

    sections: list[str] = []
    if paths:
        sections.append("Paths:\n" + "\n".join(f"- {path}" for path in paths))
    if identifiers:
        sections.append(
            "Identifiers:\n" + "\n".join(f"- {identifier}" for identifier in identifiers)
        )
    if caveats:
        sections.append("Caveats/errors:\n" + "\n".join(f"- {caveat}" for caveat in caveats))
    if not sections:
        return ""
    return "[exact retained evidence index]\n" + "\n\n".join(sections)


def _lm_studio_api_root(api_base: str) -> str:
    """Return the LM Studio native REST root for an OpenAI-compatible base URL."""

    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(api_base.rstrip("/"))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _lm_studio_headers() -> dict[str, str]:
    """Build headers for LM Studio native REST calls."""

    headers = {"Content-Type": "application/json"}
    token = (
        os.environ.get("LM_STUDIO_API_TOKEN", "").strip()
        or os.environ.get("LM_API_TOKEN", "").strip()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _release_owned_lm_studio_instance(
    app: "FastAPI",
    *,
    skip_instance_id: str = "",
    raise_on_error: bool = True,
) -> bool:
    """Unload a CLIO-owned LM Studio instance, never a user-owned one.

    CLIO records ownership only when it successfully calls LM Studio's
    native load endpoint and receives an ``instance_id``. Existing
    GUI/user-loaded instances are reused but never marked owned.
    """

    owned = getattr(app.state, "lm_studio_owned_instance", None)
    if not isinstance(owned, dict):
        return False

    instance_id = str(owned.get("instance_id") or "").strip()
    root = str(owned.get("root") or "").strip()
    if not instance_id or not root or (skip_instance_id and instance_id == skip_instance_id):
        return False

    try:
        import requests  # noqa: PLC0415

        response = requests.post(
            f"{root}/api/v1/models/unload",
            headers=_lm_studio_headers(),
            json={"instance_id": instance_id},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                "LM Studio model unload failed "
                f"({response.status_code}): {(response.text or '')[:300]}"
            )
    except Exception:
        if raise_on_error:
            raise
        return False

    app.state.lm_studio_owned_instance = None
    return True


def _coerce_error_info(value: Any) -> Optional["ErrorInfo"]:
    """Normalize agent/provider error payloads into the GACT error model."""

    if value is None:
        return None
    if isinstance(value, ErrorInfo):
        return value
    if isinstance(value, Mapping):
        raw_details = value.get("details", {})
        if isinstance(raw_details, Mapping):
            details = dict(raw_details)
        elif raw_details is None:
            details = {}
        else:
            details = {"details": raw_details}
        retry_after_raw = value.get("retry_after_s")
        retry_after_s: Optional[int] = None
        if retry_after_raw is not None:
            try:
                retry_after_s = int(retry_after_raw)
            except (TypeError, ValueError):
                details["retry_after_s"] = retry_after_raw
        return ErrorInfo(
            error=str(value.get("error") or "agent_error"),
            message=str(value.get("message") or value.get("error") or "Agent returned an error."),
            details=details,
            recoverable=bool(value.get("recoverable", True)),
            retry_after_s=retry_after_s,
        )
    return ErrorInfo(
        error="agent_error",
        message=str(value),
        recoverable=True,
    )


_EXECUTABLE_SESSION_AGENT_IDS = {
    "",
    "main",
    "default",
    "data",
    "analysis",
    "visualization",
}


class _UnsupportedSessionAgent(RuntimeError):
    """Raised when a session selects an agent CLIO cannot execute yet."""

    def __init__(
        self,
        agent_id: str,
        *,
        reason: str = "unknown_or_non_executable_agent",
        tools: list[str] | None = None,
    ) -> None:
        super().__init__(agent_id)
        self.agent_id = agent_id
        self.reason = reason
        self.tools = tools or []


class _ContextFileAccessError(RuntimeError):
    """Raised when a requested session context file cannot be prepared."""

    def __init__(self, error_info: "ErrorInfo") -> None:
        super().__init__(error_info.message)
        self.error_info = error_info


class _TurnCancelled(RuntimeError):
    """Raised internally to settle a turn as cancelled without running forward."""

    def __init__(self, error_info: "ErrorInfo") -> None:
        super().__init__(error_info.message)
        self.error_info = error_info


class _TurnTimedOut(RuntimeError):
    """Raised internally when an agent turn exceeds the configured wall clock."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"agent turn exceeded {timeout_s:g}s timeout")
        self.timeout_s = timeout_s


def _gact_turn_timeout_s() -> float:
    """Return the per-turn timeout in seconds; <=0 disables the watchdog."""

    raw = os.environ.get("CLIO_GACT_TURN_TIMEOUT_S", "300").strip()
    try:
        return float(raw)
    except ValueError:
        return 300.0


def _agent_not_available_error(app: "FastAPI", sid: str) -> "ErrorEnvelope":
    """Return a typed error when no executable CLIO agent is ready for a turn."""

    task = getattr(app.state, "agent_construction_task", None)
    task_done = bool(getattr(task, "done", lambda: True)())
    init_error = str(getattr(app.state, "agent_init_error", "") or "")
    want_agent = bool(getattr(app.state, "want_agent", False))

    if want_agent and not task_done:
        status = "starting"
        message = "CLIO is still starting its agent; no agent is ready to accept messages yet."
        recoverable = True
        recovery_actions = ["wait_for_agent_startup", "retry", "check_health"]
    elif init_error:
        status = "failed"
        message = "CLIO agent startup failed; no agent is available to accept messages."
        recoverable = True
        recovery_actions = ["check_server_logs", "fix_lm_configuration", "restart_agent"]
    else:
        status = "not_configured"
        message = (
            "No executable CLIO agent is configured for this backend. Launch `clio-agent-gact` "
            "with an LM provider configured before sending messages."
        )
        recoverable = False
        recovery_actions = ["configure_lm_provider", "restart_agent"]

    details: dict[str, Any] = {
        "session_id": sid,
        "agent_status": status,
        "want_agent": want_agent,
        "recovery_actions": recovery_actions,
    }
    if init_error:
        details["agent_init_error"] = init_error

    return ErrorEnvelope(
        error=ErrorInfo(
            error="agent_not_available",
            message=message,
            details=details,
            recoverable=recoverable,
        )
    )


def _append_session_message(app: "FastAPI", session_id: str, message: "Message") -> None:
    """Append one chronological message to memory and disk."""

    app.state.messages.setdefault(session_id, []).append(message)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.append(session_id, message)
    _mirror_workspace_messages(app, session_id)


def _extend_session_messages(
    app: "FastAPI",
    session_id: str,
    messages: list["Message"],
) -> None:
    """Append several chronological messages to memory and disk."""

    if not messages:
        return
    app.state.messages.setdefault(session_id, []).extend(messages)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.extend(session_id, messages)
    _mirror_workspace_messages(app, session_id)


def _replace_session_messages(
    app: "FastAPI",
    session_id: str,
    messages: list["Message"],
) -> None:
    """Replace one session's message ledger in memory and disk."""

    app.state.messages[session_id] = list(messages)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.replace_session(session_id, list(messages))
    _mirror_workspace_messages(app, session_id)


def _delete_session_messages(app: "FastAPI", session_id: str) -> None:
    """Remove one session's message ledger from memory and disk."""

    app.state.messages.pop(session_id, None)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.delete_session(session_id)
    _mirror_workspace_messages(app, session_id)


def _workspace_for_session(app: "FastAPI", session_id: str) -> Any | None:
    sess = app.state.sessions.get(session_id)
    if sess is None:
        return None
    return app.state.workspaces.get(getattr(sess, "workspace_id", ""))


def _workspace_storage_root_for_session(app: "FastAPI", session_id: str) -> Path | None:
    ws = _workspace_for_session(app, session_id)
    if ws is None:
        return None
    return resolve_workspace_storage_root(ws)


def _mirror_workspace_session(app: "FastAPI", session_id: str) -> None:
    """Persist one session row into the owning workspace storage root."""

    sess = app.state.sessions.get(session_id)
    root = _workspace_storage_root_for_session(app, session_id)
    if sess is None or root is None:
        return
    path = root / "sessions.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        data[session_id] = asdict(sess)
        data[session_id].setdefault("metadata", {})
        data[session_id]["metadata"]["workspace_storage_root"] = str(root)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        return


def _mirror_workspace_messages(app: "FastAPI", session_id: str) -> None:
    """Persist one message ledger into the owning workspace storage root."""

    root = _workspace_storage_root_for_session(app, session_id)
    if root is None:
        return
    try:
        store = MessageStore(root / "messages")
        messages = list(app.state.messages.get(session_id, []))
        if messages:
            store.replace_session(session_id, messages)
        else:
            store.delete_session(session_id)
    except Exception:
        return


def _remove_workspace_session_mirror(app: "FastAPI", session_id: str) -> None:
    """Remove one mirrored session row from its workspace-local store."""

    root = _workspace_storage_root_for_session(app, session_id)
    if root is None:
        return
    try:
        sessions_path = root / "sessions.json"
        if sessions_path.exists():
            data = json.loads(sessions_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.pop(session_id, None)
                tmp = sessions_path.with_suffix(sessions_path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
                os.replace(tmp, sessions_path)
        MessageStore(root / "messages").delete_session(session_id)
    except Exception:
        return


def _memory_search_terms(query: str) -> list[str]:
    """Normalize a memory search query into unique lowercase terms."""

    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_.@/-]+", query)]
    seen: set[str] = set()
    deduped: list[str] = []
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        deduped.append(term)
    return deduped


def _memory_search_excerpt(text: str, terms: list[str], *, max_chars: int = 480) -> str:
    """Return a bounded excerpt around the earliest matched term."""

    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if term and lowered.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - max_chars // 3)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def _memory_search_response(
    app: "FastAPI",
    *,
    query: str,
    session_id: str = "",
    workspace_id: str = "",
    include_cross_session: bool = False,
    limit: int = 20,
    exclude_message_id: str = "",
) -> "MemorySearchResponse":
    """Search retained GACT transcript memory with explicit scope controls."""

    terms = _memory_search_terms(query)
    if not terms:
        raise HTTPException(
            status_code=422,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_request",
                    message="memory search query must contain at least one word",
                    details={"query": query},
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    if not include_cross_session and not session_id:
        raise HTTPException(
            status_code=422,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_request",
                    message="session_id is required unless include_cross_session=true",
                    details={
                        "include_cross_session": include_cross_session,
                        "recovery_actions": [
                            "provide_session_id",
                            "set_include_cross_session",
                        ],
                    },
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )

    limit = max(1, min(int(limit or 20), 100))
    sessions = app.state.sessions.list(workspace_id=workspace_id or None)
    if session_id:
        sess = app.state.sessions.get(session_id)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {session_id}",
                        details={"session_id": session_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if include_cross_session:
            active_workspace_id = workspace_id or str(getattr(sess, "workspace_id", "") or "")
            if workspace_id and sess.workspace_id not in {workspace_id, GLOBAL_WORKSPACE_ID}:
                raise HTTPException(
                    status_code=403,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="permission_error",
                            message="memory search cannot cross workspace boundaries by default",
                            details={
                                "session_id": session_id,
                                "session_workspace_id": sess.workspace_id,
                                "requested_workspace_id": workspace_id,
                                "scope": "other_workspace",
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            if active_workspace_id and not workspace_id:
                sessions = app.state.sessions.list(workspace_id=active_workspace_id)
            session_ids = [s.id for s in sessions]
            if session_id not in session_ids:
                session_ids.append(session_id)
        else:
            session_ids = [session_id]
    else:
        session_ids = [s.id for s in sessions]

    active_workspace_id = workspace_id
    if not active_workspace_id and session_id:
        active_session = app.state.sessions.get(session_id)
        active_workspace_id = str(getattr(active_session, "workspace_id", "") or "")
    sessions_by_id = {s.id: s for s in app.state.sessions.list()}
    hits: list[MemorySearchHit] = []
    for sid in session_ids:
        sess = sessions_by_id.get(sid)
        if sess is None:
            continue
        for message in app.state.messages.get(sid, []):
            if exclude_message_id and message.id == exclude_message_id:
                continue
            for part in message.parts:
                if part.type not in {"text", "thinking", "error"}:
                    continue
                text = part.text.strip()
                if not text:
                    continue
                lowered = text.lower()
                matched = [term for term in terms if term in lowered]
                if not matched:
                    continue
                score = len(set(matched)) / len(set(terms))
                scope_label = session_scope_label(
                    active_workspace_id=active_workspace_id,
                    target_workspace_id=sess.workspace_id,
                    target_session_id=sid,
                    active_session_id=session_id,
                )
                hits.append(
                    MemorySearchHit(
                        session_id=sid,
                        session_title=sess.title,
                        workspace_id=sess.workspace_id,
                        message_id=message.id,
                        part_id=part.id,
                        role=message.role,
                        created_at=message.created_at,
                        updated_at=message.updated_at,
                        text=_memory_search_excerpt(text, matched),
                        score=round(score, 4),
                        match_terms=sorted(set(matched)),
                        metadata={
                            "cross_session": sid != session_id,
                            "source": "gact_transcript",
                            "scope": scope_label,
                            "workspace_boundary": scope_label,
                        },
                    )
                )

    hits.sort(key=lambda hit: (hit.score, hit.created_at), reverse=True)
    return MemorySearchResponse(
        query=query,
        include_cross_session=include_cross_session,
        searched_sessions=session_ids,
        hits=hits[:limit],
        metadata={
            "scope": "cross_session" if include_cross_session else "session",
            "workspace_id": active_workspace_id,
            "workspace_scope": "global" if active_workspace_id == GLOBAL_WORKSPACE_ID else "workspace",
            "limit": limit,
        },
    )


def _load_context_files(path: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """Load persisted context-file attachments keyed by session id."""

    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    sessions = data.get("sessions", {}) if isinstance(data, Mapping) else {}
    if not isinstance(sessions, Mapping):
        return {}
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    for sid, rows in sessions.items():
        if not isinstance(rows, Mapping):
            continue
        bucket: dict[str, dict[str, Any]] = {}
        for path_key, row in rows.items():
            if not isinstance(row, Mapping):
                continue
            path_value = str(row.get("path") or path_key or "").strip()
            if not path_value:
                continue
            bucket[path_value] = dict(row) | {"path": path_value}
        if bucket:
            loaded[str(sid)] = bucket
    return loaded


def _flush_context_files(app: "FastAPI") -> None:
    """Persist the current context-file ledger, if persistence is configured."""

    path = getattr(app.state, "context_files_path", None)
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"sessions": app.state.context_files}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _delete_session_context_files(app: "FastAPI", session_id: str) -> None:
    """Remove one session's context-file ledger from memory and disk."""

    if session_id in app.state.context_files:
        app.state.context_files.pop(session_id, None)
        _flush_context_files(app)


def _session_not_found(sid: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="internal_error",
                message=f"session not found: {sid}",
                details={"session_id": sid},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def _cancelled_error_info(
    sid: str,
    *,
    execution_cancellation: str,
    executor_work_may_continue: bool,
) -> "ErrorInfo":
    return ErrorInfo(
        error="cancelled",
        message="turn cancelled by client",
        details={
            "session_id": sid,
            "execution_cancellation": execution_cancellation,
            "executor_work_may_continue": executor_work_may_continue,
        },
        recoverable=True,
    )


def _cancellation_attempt_summary(attempt: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attempt:
        return {}
    return {
        key: attempt[key]
        for key in (
            "id",
            "session_id",
            "requested_at",
            "in_flight",
            "cooperative_signal_sent",
            "asyncio_task_cancel_scheduled",
            "asyncio_task_cancel_sent",
            "hard_abort_supported",
            "upstream_abort",
            "executor_work_may_continue",
        )
        if key in attempt
    }


def _enrich_cancellation_error_info(
    app: "FastAPI",
    sid: str,
    error_info: "ErrorInfo | None",
) -> "ErrorInfo | None":
    """Attach durable cancellation-attempt evidence to cancelled turns."""

    if error_info is None or error_info.error != "cancelled":
        return error_info
    attempts = getattr(app.state, "cancel_attempts", None)
    attempt = attempts.get(sid) if isinstance(attempts, Mapping) else None
    if not attempt:
        return error_info
    details = error_info.details
    details.setdefault("cancellation_attempt_id", attempt.get("id", ""))
    details.setdefault("cancellation_attempt", _cancellation_attempt_summary(attempt))
    details.setdefault("hard_abort_supported", attempt.get("hard_abort_supported", False))
    details.setdefault("upstream_abort", attempt.get("upstream_abort", "not_supported"))
    return error_info


def _context_file_access_error(
    *,
    path: str,
    mode: str,
    operation: str,
    message: str,
    original_error: BaseException | None = None,
) -> _ContextFileAccessError:
    """Build a structured GACT error for context-file preparation failures."""

    details: dict[str, Any] = {
        "path": path,
        "mode": mode,
        "operation": operation,
        "recovery_actions": [
            "reattach_context_file",
            "remove_context_file",
            "retry",
            "exit",
        ],
    }
    if original_error is not None:
        details["original_error"] = type(original_error).__name__
        details["original_message"] = str(original_error)
    return _ContextFileAccessError(
        ErrorInfo(
            error="context_file_error",
            message=message,
            details=details,
            recoverable=True,
        )
    )


def _session_agent_id(sess: Any) -> str:
    """Return the active session agent id from dict or object refs."""

    agent = getattr(sess, "agent", None)
    if isinstance(agent, Mapping):
        return str(agent.get("id") or "").strip()
    return str(getattr(agent, "id", "") or "").strip()


def _merge_agent_def_rows(rows: list["AgentDef"]) -> list["AgentDef"]:
    """Resolve agent rows by id while preserving provenance of overridden rows."""

    merged: dict[str, AgentDef] = {}
    chains: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        chain = chains.setdefault(row.id, [])
        if row.id in merged:
            prior = merged[row.id]
            chain.append(
                {
                    "source": prior.source,
                    "scope": str(
                        prior.metadata.get("expert_scope")
                        or prior.metadata.get("pack_scope")
                        or ""
                    ),
                    "pack_id": str(prior.metadata.get("pack_id") or ""),
                    "definition_path": str(
                        prior.metadata.get("definition_path")
                        or prior.metadata.get("expert_path")
                        or ""
                    ),
                }
            )
        current = {
            "source": row.source,
            "scope": str(
                row.metadata.get("expert_scope") or row.metadata.get("pack_scope") or ""
            ),
            "pack_id": str(row.metadata.get("pack_id") or ""),
            "definition_path": str(
                row.metadata.get("definition_path") or row.metadata.get("expert_path") or ""
            ),
        }
        merged[row.id] = row.model_copy(
            update={"metadata": {**row.metadata, "override_chain": [*chain, current]}}
        )
    return list(merged.values())


def _resolve_dynamic_agent(app: "FastAPI", agent_id: str) -> "AgentDef | None":
    """Return a registered user/skill/builtin/expert-pack agent definition by id."""
    if not agent_id:
        return None
    row = app.state.user_agents.get(agent_id)
    if row is not None:
        return _apply_prompt_registry_to_agent(app, AgentDef(**row.to_wire()))
    for skill in _load_skills_from_disk():
        if skill.id == agent_id:
            return _apply_prompt_registry_to_agent(app, skill)
    expert_rows = validate_expert_hierarchy(
        _merge_agent_def_rows(_builtin_agents() + load_expert_packs())
    )
    for expert in expert_rows:
        if expert.id == agent_id and expert.enabled:
            return _apply_prompt_registry_to_agent(app, expert)
    return None


def _agent_prompt_request(agent_def: "AgentDef") -> tuple[str, str]:
    """Return prompt id/profile requested by an agent definition."""

    metadata = agent_def.metadata if isinstance(agent_def.metadata, Mapping) else {}
    params = agent_def.parameters if isinstance(agent_def.parameters, Mapping) else {}
    prompt_id = str(
        metadata.get("prompt_id")
        or metadata.get("prompt")
        or params.get("prompt_id")
        or params.get("prompt")
        or ""
    ).strip()
    prompt_profile = str(
        metadata.get("prompt_profile")
        or metadata.get("profile")
        or params.get("prompt_profile")
        or params.get("profile")
        or ""
    ).strip()
    return prompt_id, prompt_profile


def _prompt_resolution_metadata(resolved: Any, *, requested_profile: str = "") -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "id": getattr(resolved, "id", ""),
            "profile": getattr(resolved, "profile", ""),
            "requested_profile": requested_profile,
            "scope": getattr(resolved, "scope", ""),
            "source_path": getattr(resolved, "source_path", ""),
            "provider": getattr(resolved, "provider", ""),
            "model": getattr(resolved, "model", ""),
            "checksum": getattr(resolved, "checksum", ""),
            "fallback_profile": getattr(resolved, "fallback_profile", ""),
            "validation_errors": list(getattr(resolved, "validation_errors", []) or []),
        }.items()
        if v not in ("", [], None)
    }


def _apply_prompt_registry_to_agent(app: "FastAPI", agent_def: "AgentDef") -> "AgentDef":
    """Resolve an agent's prompt registry reference into runtime prompt text."""

    prompt_id, prompt_profile = _agent_prompt_request(agent_def)
    if not prompt_id:
        return agent_def
    registry = getattr(app.state, "prompt_registry", None)
    if registry is None:
        return agent_def
    resolved = registry.resolve(prompt_id, profile=prompt_profile)
    metadata = dict(agent_def.metadata)
    if resolved is None:
        metadata["prompt_resolution"] = {
            "id": prompt_id,
            "requested_profile": prompt_profile,
            "status": "missing",
        }
        return agent_def.model_copy(update={"metadata": metadata})
    resolution = _prompt_resolution_metadata(resolved, requested_profile=prompt_profile)
    resolution["status"] = "resolved" if resolved.text.strip() else "invalid"
    metadata["prompt_resolution"] = resolution
    updates: dict[str, Any] = {"metadata": metadata}
    if resolved.text.strip():
        updates["system_prompt"] = resolved.text
    if resolved.provider:
        updates["default_provider"] = resolved.provider
    if resolved.model:
        updates["default_model"] = resolved.model
    return agent_def.model_copy(update=updates)


def _prompt_render_context(app: "FastAPI") -> dict[str, str]:
    """Build the CLIO-owned dynamic context exposed to prompt templates."""

    try:
        agents = validate_expert_hierarchy(
            _merge_agent_def_rows(
                _builtin_agents()
                + [AgentDef(**row.to_wire()) for row in app.state.user_agents.list()]
                + _load_skills_from_disk()
                + load_expert_packs()
            )
        )
    except Exception:
        agents = _builtin_agents()
    enabled_agents = [agent for agent in agents if getattr(agent, "enabled", True)]
    by_parent: dict[str, list["AgentDef"]] = {}
    for agent in enabled_agents:
        by_parent.setdefault(agent.parent_id or "", []).append(agent)

    def render_tree(parent_id: str = "", depth: int = 0) -> list[str]:
        lines: list[str] = []
        for agent in sorted(by_parent.get(parent_id, []), key=lambda row: (row.tier, row.id)):
            indent = "  " * depth
            detail = f" - {agent.description}" if agent.description else ""
            lines.append(f"{indent}- {agent.id}: {agent.title}{detail}")
            lines.extend(render_tree(agent.id, depth + 1))
        return lines

    flat_agents = [
        f"- {agent.id}: {agent.title}" for agent in sorted(enabled_agents, key=lambda row: row.id)
    ]
    tools = [f"- {tool.id}: {tool.description}" for tool in _builtin_tools()]
    commands: list[str] = []
    try:
        for row in _load_command_files_from_disk():
            if row.get("enabled") and row.get("agent_invocable"):
                commands.append(f"- {row.get('id')}: {row.get('description') or row.get('title')}")
    except Exception:
        commands = []
    provider = getattr(app.state, "lm_config", None)
    provider_summary = "{}"
    if provider is not None:
        try:
            provider_summary = json.dumps(asdict(provider), sort_keys=True)
        except Exception:
            provider_summary = str(provider)
    return {
        "agents.available_tree": "\n".join(render_tree()) or "(no enabled experts)",
        "agents.available_flat": "\n".join(flat_agents) or "(no enabled experts)",
        "tools.available": "\n".join(tools) or "(no declared tools)",
        "commands.agent_invocable": "\n".join(commands) or "(no agent-invocable commands)",
        "memory.policy_summary": "Session-local by default; same-workspace/global reads require explicit policy or user intent.",
        "permissions.policy_summary": "Permission-controlled actions must use CLIO policy gates and visible provenance.",
        "provider.current": provider_summary,
        "session.active_pack": "(no active expert pack)",
    }


def _keyword_routed_user_agent(app: "FastAPI", text: str) -> "AgentDef | None":
    """Return the best registered user agent whose keyword matches text.

    This intentionally ignores auto-discovered skills for now. Skills can be
    numerous and global, so implicit routing only uses agents the user
    registered directly in this CLIO backend.
    """

    normalized = f" {re.sub(r'[^a-z0-9_+-]+', ' ', text.lower())} "
    matches: list[tuple[int, str, AgentDef]] = []
    for row in app.state.user_agents.list():
        agent = AgentDef(**row.to_wire())
        for raw_keyword in agent.keywords:
            keyword = str(raw_keyword or "").strip().lower()
            if not keyword:
                continue
            needle = f" {re.sub(r'[^a-z0-9_+-]+', ' ', keyword)} "
            if needle.strip() and needle in normalized:
                matches.append((len(keyword), agent.id, agent))
                break
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]))
    return matches[0][2]


def _dynamic_agent_runtime_provenance(
    app: "FastAPI",
    agent_def: "AgentDef",
    *,
    execution_mode: str,
) -> dict[str, Any]:
    """Return non-secret provenance for the dynamic agent used this turn."""

    active_model = _active_lm_model_ref(app)
    provider_id = agent_def.default_provider or active_model.get("provider_id", "")
    model_id = agent_def.default_model or active_model.get("model_id", "")
    payload: dict[str, Any] = {
        "kind": "dynamic_agent",
        "agent_id": agent_def.id,
        "source": agent_def.source,
        "title": agent_def.title,
        "execution_mode": execution_mode,
        "tools": list(agent_def.tools),
        "prompt": {
            "source": "agent_definition",
            "has_system_prompt": bool(agent_def.system_prompt.strip()),
        },
        "model": {
            "provider_id": provider_id,
            "model_id": model_id,
            "provider_source": (
                "agent_default" if agent_def.default_provider else "global_active"
            ),
            "model_source": "agent_default" if agent_def.default_model else "global_active",
            "fallback_to_global": not (agent_def.default_provider and agent_def.default_model),
        },
    }
    if agent_def.source == "expert_pack":
        payload.update(
            {
                "parent_id": agent_def.parent_id,
                "skills": list(agent_def.skills),
                "commands": list(agent_def.commands),
                "pack": {
                    "id": str(agent_def.metadata.get("pack_id") or ""),
                    "version": str(agent_def.metadata.get("pack_version") or ""),
                    "scope": str(
                        agent_def.metadata.get("pack_scope")
                        or agent_def.metadata.get("expert_scope")
                        or ""
                    ),
                    "definition_path": str(
                        agent_def.metadata.get("definition_path")
                        or agent_def.metadata.get("pack_definition_path")
                        or agent_def.metadata.get("expert_path")
                        or ""
                    ),
                },
            }
        )
    return payload


def _delegated_expert_agent_id(row: Mapping[str, Any]) -> str:
    """Return the requested delegated expert id from a handoff row."""

    for key in ("delegate_to", "agent_id", "target_agent_id", "expert"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _delegated_expert_prompt(row: Mapping[str, Any], fallback: str) -> str:
    for key in ("question", "input", "prompt", "request"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return fallback


def _should_execute_delegated_handoff(row: Mapping[str, Any]) -> bool:
    if row.get("execute") is False:
        return False
    if row.get("execute") is True or row.get("delegate_to") or row.get("target_agent_id"):
        return True
    status = str(row.get("status") or "").strip().lower()
    return status in {"requested", "pending", "delegate", "delegated"}


def _user_agent_param(agent_def: "AgentDef", name: str) -> Any:
    """Return one user-agent generation parameter, if present."""
    params = agent_def.parameters if isinstance(agent_def.parameters, Mapping) else {}
    return params.get(name)


def _user_agent_int_param(agent_def: "AgentDef", name: str, default: int) -> int:
    """Parse an integer user-agent parameter with an explicit error."""
    value = _user_agent_param(agent_def, name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user agent parameter {name!r} must be an integer") from exc


def _user_agent_float_param(agent_def: "AgentDef", name: str, default: float) -> float:
    """Parse a float user-agent parameter with an explicit error."""
    value = _user_agent_param(agent_def, name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user agent parameter {name!r} must be a number") from exc


def _dynamic_agent_lm_config(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Build a provider config for a registered dynamic agent."""
    from clio_agent.config import (  # noqa: PLC0415
        LMProviderConfig,
        load_config_from_env,
    )

    base_config = getattr(base_agent, "_provider_config", None)
    if base_config is None:
        base_config = load_config_from_env()
    provider = agent_def.default_provider or base_config.provider
    same_provider = provider == base_config.provider
    params = agent_def.parameters if isinstance(agent_def.parameters, Mapping) else {}
    api_base = str(params.get("api_base") or (base_config.api_base if same_provider else ""))
    api_key = base_config.api_key if same_provider else ""
    return LMProviderConfig(
        provider=provider,  # type: ignore[arg-type]
        api_base=api_base,
        model=agent_def.default_model or (base_config.model if same_provider else ""),
        api_key=api_key,
        temperature=_user_agent_float_param(agent_def, "temperature", base_config.temperature),
        max_tokens=_user_agent_int_param(agent_def, "max_tokens", base_config.max_tokens),
        planner_temperature=base_config.planner_temperature,
        planner_max_tokens=base_config.planner_max_tokens,
        codex_transport=base_config.codex_transport,
        thinking_budget=_user_agent_int_param(
            agent_def,
            "thinking_budget",
            base_config.thinking_budget,
        ),
    )


def _prompt_user_agent_signature() -> Any:
    """Return the DSPy signature used by prompt-only dynamic agents."""
    import dspy  # noqa: PLC0415

    class PromptUserAgentSignature(dspy.Signature):
        """Run a registered CLIO user agent.

        Follow the supplied system_prompt exactly. Answer from the
        prompt and user request. Do not claim to have called tools.
        If a requested fact requires unavailable tool execution, say
        what is missing instead of inventing it.
        """

        system_prompt: str = dspy.InputField(desc="Registered agent instructions")
        question: str = dspy.InputField(desc="User message for this agent")
        answer: str = dspy.OutputField(desc="User-facing answer")

    return PromptUserAgentSignature


def _build_prompt_user_agent_module(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Build a DSPy module wrapper for a streamable prompt-only dynamic agent."""
    import dspy  # noqa: PLC0415

    from clio_agent.config import (  # noqa: PLC0415
        create_chat_adapter,
        create_lm,
    )

    class PromptUserAgentModule(dspy.Module):
        def __init__(self, base_agent: Any, agent_def: "AgentDef") -> None:
            super().__init__()
            self.agent_def = agent_def
            self.config = _dynamic_agent_lm_config(base_agent, agent_def)
            self.system_prompt = agent_def.system_prompt.strip() or (
                f"You are the CLIO user agent {agent_def.id!r}. "
                "Answer directly and do not invent tool results."
            )
            self.answer_synthesizer = dspy.Predict(_prompt_user_agent_signature())

        def forward(
            self,
            question: str,
            session_id: str,
            session_mode: str = "chat",
            session_edit_mode: str = "diff",
            cancel_requested: Any | None = None,
        ) -> Any:
            del session_mode, session_edit_mode
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            with dspy.context(
                lm=create_lm(self.config),
                adapter=create_chat_adapter(self.config),
            ):
                result = self.answer_synthesizer(
                    system_prompt=self.system_prompt,
                    question=question,
                )
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            answer = str(getattr(result, "answer", "") or "").strip()
            if not answer:
                raise RuntimeError(f"user agent {self.agent_def.id!r} returned an empty answer")
            return dspy.Prediction(
                answer=answer,
                selected_expert=self.agent_def.id,
                routing_rationale=f"Session selected user agent {self.agent_def.id!r}.",
                route_source="user_agent",
                session_id=session_id,
                error_info=None,
            )

    return PromptUserAgentModule(base_agent, agent_def)


def _tool_user_agent_signature() -> Any:
    """Return the DSPy signature used by tool-declaring dynamic agents."""
    import dspy  # noqa: PLC0415

    class ToolUserAgentSignature(dspy.Signature):
        """Run a registered CLIO user agent with its declared MCP tools.

        Follow the supplied system_prompt exactly. Use only the tools
        made available to this agent. Surface tool failures explicitly
        instead of inventing results.
        """

        system_prompt: str = dspy.InputField(desc="Registered agent instructions")
        question: str = dspy.InputField(desc="User message for this agent")
        answer: str = dspy.OutputField(desc="User-facing answer")

    return ToolUserAgentSignature


def _dynamic_agent_tools(base_agent: Any, agent_def: "AgentDef") -> list[Any]:
    """Resolve the exact DSPy tools a tool-declaring dynamic agent may use."""
    requested_tools = [str(t).strip() for t in agent_def.tools if str(t).strip()]
    tool_executor = getattr(base_agent, "tool_executor", None)
    if tool_executor is None or not hasattr(tool_executor, "to_dspy_tools"):
        raise _UnsupportedSessionAgent(
            agent_def.id,
            reason="custom_agent_tool_executor_unavailable",
            tools=requested_tools,
        )

    available_tools = {
        str(getattr(tool, "name", "")): tool
        for tool in list(tool_executor.to_dspy_tools())
        if getattr(tool, "name", "")
    }
    missing_tools = [name for name in requested_tools if name not in available_tools]
    if missing_tools:
        raise _UnsupportedSessionAgent(
            agent_def.id,
            reason="custom_agent_tools_unavailable",
            tools=missing_tools,
        )
    return [available_tools[name] for name in requested_tools]


def _tool_user_agent_max_iters(agent_def: "AgentDef") -> int:
    max_iters = _user_agent_int_param(agent_def, "max_iters", 5)
    if max_iters <= 0:
        raise ValueError("user agent parameter 'max_iters' must be positive")
    return max_iters


def _build_tool_user_agent_module(base_agent: Any, agent_def: "AgentDef") -> Any:
    """Build a DSPy ReAct wrapper for a streamable tool-declaring dynamic agent."""
    import dspy  # noqa: PLC0415

    from clio_agent.config import (  # noqa: PLC0415
        create_chat_adapter,
        create_lm,
    )

    class ToolUserAgentModule(dspy.Module):
        def __init__(self, base_agent: Any, agent_def: "AgentDef") -> None:
            super().__init__()
            self.agent_def = agent_def
            self.config = _dynamic_agent_lm_config(base_agent, agent_def)
            self.tools = _dynamic_agent_tools(base_agent, agent_def)
            self.system_prompt = agent_def.system_prompt.strip() or agent_def.description
            self.react_agent = dspy.ReAct(
                _tool_user_agent_signature(),
                tools=self.tools,
                max_iters=_tool_user_agent_max_iters(agent_def),
            )
            self.answer_synthesizer = self.react_agent.extract.predict

        def forward(
            self,
            question: str,
            session_id: str,
            session_mode: str = "chat",
            session_edit_mode: str = "diff",
            cancel_requested: Any | None = None,
        ) -> Any:
            del session_mode, session_edit_mode
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            with dspy.context(
                lm=create_lm(self.config),
                adapter=create_chat_adapter(self.config),
            ):
                result = self.react_agent(
                    system_prompt=self.system_prompt,
                    question=question,
                )
            if cancel_requested is not None and cancel_requested():
                raise _TurnCancelled(
                    _cancelled_error_info(
                        session_id,
                        execution_cancellation="cooperative",
                        executor_work_may_continue=False,
                    )
                )
            answer = str(getattr(result, "answer", "") or "").strip()
            if not answer:
                raise RuntimeError(f"user agent {self.agent_def.id!r} returned an empty answer")
            return dspy.Prediction(
                answer=answer,
                selected_expert=self.agent_def.id,
                routing_rationale=f"Session selected tool user agent {self.agent_def.id!r}.",
                route_source="user_agent",
                session_id=session_id,
                trajectory=getattr(result, "trajectory", None),
                error_info=None,
            )

    return ToolUserAgentModule(base_agent, agent_def)


def _run_prompt_user_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
) -> Any:
    """Execute a prompt-only user/skill agent through DSPy/LiteLLM."""
    module = _build_prompt_user_agent_module(base_agent, agent_def)
    return module.forward(
        question=question,
        session_id=session_id,
        cancel_requested=cancel_requested,
    )


def _run_tool_user_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
) -> Any:
    """Execute a tool-declaring user/skill agent through DSPy ReAct."""
    module = _build_tool_user_agent_module(base_agent, agent_def)
    return module.forward(
        question=question,
        session_id=session_id,
        cancel_requested=cancel_requested,
    )


def _tool_call_event_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a stable identity for de-duplicating tool telemetry events."""
    name = str(call.get("name") or call.get("tool") or "")
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    try:
        encoded_args = json.dumps(args or {}, sort_keys=True, default=str)
    except TypeError:
        encoded_args = str(args or {})
    return name, encoded_args


def _model_ref_dict(value: Any) -> dict[str, str]:
    """Normalize a GACT ModelRef-like value to its wire keys."""

    if value is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    elif hasattr(value, "model_dump"):
        raw = value.model_dump(exclude_none=True)
    else:
        raw = {
            "provider_id": getattr(value, "provider_id", ""),
            "model_id": getattr(value, "model_id", ""),
            "variant": getattr(value, "variant", ""),
        }
    return {
        "provider_id": str(raw.get("provider_id") or raw.get("provider") or ""),
        "model_id": str(raw.get("model_id") or raw.get("model") or ""),
        "variant": str(raw.get("variant") or ""),
    }


def _model_ref_is_empty(value: Any) -> bool:
    """Return true when a model ref carries no selection."""

    ref = _model_ref_dict(value)
    return not any(ref.values())


def _effective_lm_config(app: "FastAPI") -> dict[str, Any]:
    """Return the configured LM, falling back to the live agent config.

    ``app.state.lm_config`` is populated by ``PUT /v1/providers/lm``.
    When GACT boots from ``CLIO_LM_PROVIDER`` instead, the live
    ``ClioAgent`` still carries the effective ``LMProviderConfig``.
    """

    cfg = dict(getattr(app.state, "lm_config", None) or {})
    agent = getattr(app.state, "agent", None)
    provider_config = getattr(agent, "_provider_config", None)
    if provider_config is None:
        return cfg

    for key in (
        "provider",
        "api_base",
        "model",
        "temperature",
        "max_tokens",
        "context_length",
        "thinking_budget",
    ):
        if not cfg.get(key):
            value = getattr(provider_config, key, None)
            if value is not None:
                cfg[key] = value
    if not cfg.get("transport") and getattr(provider_config, "provider", "") == "codex":
        cfg["transport"] = getattr(provider_config, "codex_transport", None)
    return cfg


def _active_lm_model_ref(app: "FastAPI") -> dict[str, str]:
    """Return the active global LM as a GACT ModelRef-shaped dict."""

    cfg = _effective_lm_config(app)
    provider = str(cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    return {"provider_id": provider, "model_id": model, "variant": ""}


def _model_ref_matches_active(value: Any, app: "FastAPI") -> bool:
    """Return true when a requested model ref exactly matches the active LM."""

    return _model_ref_dict(value) == _active_lm_model_ref(app)


def _clear_session_model_refs(app: "FastAPI") -> None:
    """Clear per-session model refs after a global LM provider swap.

    CLIO executes every turn through the active global LM. Existing
    sessions may still carry stale GACT ModelRefs from older TUI
    versions or emulator-compatible defaults; leaving those refs in
    place makes the next send fail with a per-session override error
    even though the user just changed the global provider correctly.
    """

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return
    for sess in sessions.list():
        if not _model_ref_is_empty(sess.model):
            sessions.update(sess.id, model={})


def _unsupported_model_ref_error(
    *,
    session_id: str,
    source: str,
    model_ref: Any,
    active_model: Mapping[str, str],
) -> ErrorEnvelope:
    """Build a structured error for currently unsupported model refs."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="not_implemented",
            message=(
                f"{source} model overrides are not implemented for a model "
                "that differs from the active global LM."
            ),
            details={
                "session_id": session_id,
                "source": source,
                "model": _model_ref_dict(model_ref),
                "active_model": dict(active_model),
                "recovery_actions": [
                    "put_global_lm_provider",
                    "clear_session_model",
                    "retry",
                    "exit",
                ],
            },
            recoverable=True,
        )
    )


async def _run_turn_in_background(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
    turn_agent_id: str = "",
) -> None:
    """Drive an agent turn off the request thread.

    The POST handler returns immediately after staging the user
    message; this coroutine handles the rest: invoking forward() in
    an executor, slicing the result into Parts, publishing every
    SSE event the TUI consumes, persisting the assistant message,
    and settling the session back to idle (or error).

    Errors here are *consumed* — they emit a message.completed with
    error_info and a session.status_changed → error so the TUI sees
    the failure live. We never re-raise; the request that started us
    is long gone.
    """

    bus: EventBus = app.state.bus
    sess = app.state.sessions.get(sid)
    if sess is None:
        # Session evaporated between POST + background start; can't
        # do anything useful. Don't raise — the publishing path
        # would crash and pollute logs with no client to notify.
        return

    error_info: Optional[ErrorInfo] = None
    answer_text = ""
    selected_agent = ""
    rationale = ""
    route_source = ""
    route_reason = ""
    auto_routed_agent: "AgentDef | None" = None
    agent_runtime: dict[str, Any] = {}
    dynamic_agent_used: "AgentDef | None" = None
    execution_path = ""
    tools_called: list[dict[str, Any]] = []
    expert_handoffs: list[dict[str, Any]] = []
    prompt_resolution: dict[str, Any] = {}
    proposed_diffs: list[Any] = []
    nanoagents: list[Any] = []
    thinking_text = ""
    retry_attempt_id = ""
    if isinstance(user_msg.metadata, dict):
        retry_attempt_id = str(user_msg.metadata.get("retry_attempt_id") or "")
    turn_tokens: dict[str, int] = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    turn_cost = 0.0

    def _update_retry_attempt(
        status: str,
        *,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> None:
        if not retry_attempt_id:
            return
        attempt = app.state.turn_attempts.get(retry_attempt_id)
        if attempt is None:
            return
        metadata = dict(attempt.metadata)
        if metadata_patch:
            metadata.update(metadata_patch)
        updated = attempt.model_copy(
            update={
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata,
            }
        )
        app.state.turn_attempts[retry_attempt_id] = updated
        app.state.bus.publish(
            Event(
                type=f"turn.retry_{status}",
                session_id=sid,
                payload=updated.model_dump(exclude_none=True),
            )
        )

    if retry_attempt_id:
        _update_retry_attempt(
            "running",
            metadata_patch={"executed_user_message_id": user_msg.id},
        )

    # iowarp/clio-agent#5: prepend any attached context files to the
    # user's text so the agent's forward() sees them as primed input.
    # Plain text concat — keeps the agent.py interface untouched and
    # works regardless of which expert handles the turn.
    context_file_error: ErrorInfo | None = None
    context_file_provenance = _context_file_turn_provenance(app, sid, status="prepared")
    memory_search_metadata: dict[str, Any] = {}
    try:
        enriched_text = _enrich_with_context_files(app, sid, user_text)
        enriched_text, memory_search_metadata = _enrich_with_requested_memory_search(
            app,
            sid,
            enriched_text,
            user_msg,
        )
    except _ContextFileAccessError as exc:
        enriched_text = user_text
        context_file_error = exc.error_info
        context_file_provenance = _context_file_turn_provenance(app, sid, status="error")
    context_frame = _record_context_frame(
        app,
        sid,
        sess,
        user_msg,
        user_text=user_text,
        enriched_text=enriched_text,
        context_error=context_file_error,
    )
    # iowarp/clio-agent#20: pre_message hook can transform the
    # input or veto the turn. PermissionError → cancelled-style
    # error_info; the caller sees the hook's reason.
    if context_file_error is None:
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _fire_hook("pre_message", sid, enriched_text)
        except PermissionError as exc:
            bus.publish(
                Event(
                    type="message.completed",
                    session_id=sid,
                    payload={
                        "message_id": user_msg.id,
                        "stop_reason": "blocked",
                        "error_info": {
                            "error": "permission_error",
                            "message": str(exc),
                            "recoverable": True,
                        },
                    },
                )
            )
            app.state.sessions.update(sid, status="error")
            _update_retry_attempt(
                "failed",
                metadata_patch={
                    "execution_error": "permission_error",
                    "executed_user_message_id": user_msg.id,
                },
            )
            bus.publish(
                Event(
                    type="session.status_changed",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "status": "error",
                        "prev_status": "running",
                        "reason": "pre_message hook blocked turn",
                    },
                )
            )
            return

    # iowarp/clio-agent#6: try real per-token streaming via
    # dspy.streamify when the LM supports it; fall back to the
    # synchronous executor path otherwise. Streaming produces
    # message.part.delta events as chunks arrive — without it the
    # text part lands as one big delta after forward returns.
    streamed_assistant_part_id: Optional[str] = None
    streamed_assistant_buffer: list[str] = []
    streamed_assistant_msg_id: Optional[str] = None

    async def _emit_chunk(text: str) -> None:
        nonlocal streamed_assistant_part_id, streamed_assistant_msg_id
        if streamed_assistant_msg_id is None:
            # Lazily invent ids the moment the first chunk arrives;
            # the final assistant message will reuse them.
            streamed_assistant_msg_id = _new_message_id("asst")
            streamed_assistant_part_id = _new_part_id()
            bus.publish(
                Event(
                    type="message.created",
                    session_id=sid,
                    payload=Message(
                        id=streamed_assistant_msg_id,
                        session_id=sid,
                        role="assistant",
                        created_at=_iso_from_epoch(time.time()),
                        updated_at=_iso_from_epoch(time.time()),
                        parts=[],
                    ).model_dump(exclude_none=True),
                )
            )
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "message_id": streamed_assistant_msg_id,
                        "part": Part(
                            id=streamed_assistant_part_id,
                            type="text",
                            text="",
                            metadata={"stream_source": "live"},
                        ).model_dump(exclude_none=True),
                    },
                )
            )
        streamed_assistant_buffer.append(text)
        bus.publish(
            Event(
                type="message.part.delta",
                session_id=sid,
                payload={
                    "message_id": streamed_assistant_msg_id,
                    "part_id": streamed_assistant_part_id,
                    "stream_source": "live",
                    "delta": {"text_append": text},
                },
            )
        )

    # iowarp/clio-agent#8: snapshot LM history before the turn so we
    # can sum every call this turn made. ContextVars don't propagate
    # to asyncio executor threads (so dspy.settings.usage_tracker is
    # unreliable from worker threads), but ``lm.history`` IS shared
    # across threads — list.append under the GIL gives us a clean,
    # thread-safe ledger. We diff history[start:end] post-turn.
    history_start = _snapshot_lm_history_index(app)
    _pop_stream_fallback(app, sid)
    turn_cancel_event = threading.Event()
    app.state.cancel_events[sid] = turn_cancel_event
    if sid in app.state.cancel_flags:
        turn_cancel_event.set()
    turn_timeout_s = _gact_turn_timeout_s()
    turn_deadline = time.monotonic() + turn_timeout_s if turn_timeout_s > 0 else 0.0

    def cancel_requested() -> bool:
        return turn_cancel_event.is_set()

    async def _await_turn_work(awaitable: Any) -> Any:
        if turn_timeout_s <= 0:
            return await awaitable
        remaining = turn_deadline - time.monotonic()
        if remaining <= 0:
            turn_cancel_event.set()
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise _TurnTimedOut(turn_timeout_s)
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError as exc:
            turn_cancel_event.set()
            raise _TurnTimedOut(turn_timeout_s) from exc

    async def _run_dynamic_agent_sync(agent_def: "AgentDef", prompt: str) -> Any:
        runner = _run_tool_user_agent if agent_def.tools else _run_prompt_user_agent
        loop = asyncio.get_running_loop()
        turn_context = contextvars.copy_context()
        return await _await_turn_work(
            loop.run_in_executor(
                None,
                lambda: turn_context.run(
                    _run_dynamic_agent_compat,
                    runner,
                    app.state.agent,
                    agent_def,
                    prompt,
                    sid,
                    cancel_requested,
                ),
            ),
        )

    async def _execute_delegated_experts(
        parent_agent: "AgentDef",
        rows: list[dict[str, Any]],
        *,
        source_text: str,
        depth: int = 0,
        seen: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        if seen is None:
            seen = {parent_agent.id}
        if depth >= 3:
            return [
                {
                    **row,
                    "status": "skipped",
                    "skip_reason": "max_delegate_depth_reached",
                    "parent_id": parent_agent.id,
                    "depth": depth,
                }
                for row in rows
                if _should_execute_delegated_handoff(row)
            ]

        executed: list[dict[str, Any]] = []
        for row in rows:
            if not _should_execute_delegated_handoff(row):
                executed.append(row)
                continue
            target_id = _delegated_expert_agent_id(row)
            if not target_id:
                executed.append(
                    {
                        **row,
                        "status": "skipped",
                        "skip_reason": "missing_delegate_target",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue
            target = _resolve_dynamic_agent(app, target_id)
            if target is None or target.source != "expert_pack" or not target.enabled:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_not_available",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue
            if target.parent_id != parent_agent.id:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_parent_mismatch",
                        "parent_id": parent_agent.id,
                        "target_parent_id": target.parent_id,
                        "depth": depth,
                    }
                )
                continue
            if target.id in seen:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_cycle_detected",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue

            prompt = _delegated_expert_prompt(row, source_text)
            execution_mode = "tool_agent" if target.tools else "prompt_agent"
            started_at = time.perf_counter()
            try:
                pred_child = await _run_dynamic_agent_sync(target, prompt)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                output = str(getattr(pred_child, "answer", "") or "").strip()
                child_rows: list[dict[str, Any]] = []
                raw_child_rows = getattr(pred_child, "expert_handoffs", None) or []
                if isinstance(raw_child_rows, list):
                    child_rows = [
                        dict(child)
                        for child in raw_child_rows
                        if isinstance(child, dict)
                    ]
                nested = await _execute_delegated_experts(
                    target,
                    child_rows,
                    source_text=prompt,
                    depth=depth + 1,
                    seen={*seen, target.id},
                )
                executed.append(
                    {
                        **row,
                        "agent_id": target.id,
                        "parent_id": parent_agent.id,
                        "pack_id": str(target.metadata.get("pack_id") or ""),
                        "pack_version": str(target.metadata.get("pack_version") or ""),
                        "provider_id": target.default_provider,
                        "model_id": target.default_model,
                        "fallback_warnings": list(target.validation_errors),
                        "status": "completed",
                        "depth": depth,
                        "duration_ms": duration_ms,
                        "execution_mode": execution_mode,
                        "input": prompt,
                        "output_summary": output[:500],
                        "children": nested,
                    }
                )
            except (_TurnCancelled, _TurnTimedOut):
                raise
            except Exception as exc:  # noqa: BLE001
                executed.append(
                    {
                        **row,
                        "agent_id": target.id,
                        "parent_id": parent_agent.id,
                        "pack_id": str(target.metadata.get("pack_id") or ""),
                        "pack_version": str(target.metadata.get("pack_version") or ""),
                        "provider_id": target.default_provider,
                        "model_id": target.default_model,
                        "fallback_warnings": list(target.validation_errors),
                        "status": "failed",
                        "depth": depth,
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                        "execution_mode": execution_mode,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
        return executed

    try:
        if context_file_error is not None:
            raise _ContextFileAccessError(context_file_error)

        if sid in app.state.cancel_flags:
            app.state.cancel_flags.discard(sid)
            raise _TurnCancelled(
                _cancelled_error_info(
                    sid,
                    execution_cancellation="turn_boundary",
                    executor_work_may_continue=False,
                )
            )

        session_agent_id = _session_agent_id(sess)
        active_agent_id = turn_agent_id or session_agent_id
        routing_mode = getattr(sess, "routing_mode", "auto") or "auto"
        auto_routed_agent = None
        if (
            not turn_agent_id
            and active_agent_id in {"", "main", "default"}
            and routing_mode in {"auto", "experts"}
        ):
            auto_routed_agent = _keyword_routed_user_agent(app, user_text)
            if auto_routed_agent is not None:
                active_agent_id = auto_routed_agent.id
        from clio_agent.agent import cancellation_checker as _cancellation_checker  # noqa: PLC0415

        _refresh_argonne_lm_token(app.state.agent)

        if active_agent_id not in _EXECUTABLE_SESSION_AGENT_IDS:
            dynamic_agent = _resolve_dynamic_agent(app, active_agent_id)
            if dynamic_agent is None:
                raise _UnsupportedSessionAgent(active_agent_id)
            prompt_resolution = dict(dynamic_agent.metadata.get("prompt_resolution") or {})
            dynamic_agent_used = dynamic_agent
            runner = _run_tool_user_agent if dynamic_agent.tools else _run_prompt_user_agent
            execution_mode = "tool_agent" if dynamic_agent.tools else "prompt_agent"
            agent_runtime = _dynamic_agent_runtime_provenance(
                app,
                dynamic_agent,
                execution_mode=execution_mode,
            )
            module = (
                _build_tool_user_agent_module(app.state.agent, dynamic_agent)
                if dynamic_agent.tools
                else _build_prompt_user_agent_module(app.state.agent, dynamic_agent)
            )
            with _cancellation_checker(cancel_requested), _tool_session_context(sid):
                pred = await _await_turn_work(
                    _try_streamed_forward_compat(
                        app,
                        enriched_text,
                        sid,
                        _emit_chunk,
                        session_mode=getattr(sess, "mode", "chat"),
                        session_edit_mode=getattr(sess, "edit_mode", "diff"),
                        agent_override=module,
                        cancel_requested=cancel_requested,
                    )
                )
            if pred is None:
                with _cancellation_checker(cancel_requested), _tool_session_context(sid):
                    loop = asyncio.get_running_loop()
                    turn_context = contextvars.copy_context()
                    pred = await _await_turn_work(
                        loop.run_in_executor(
                            None,
                            lambda: turn_context.run(
                                _run_dynamic_agent_compat,
                                runner,
                                app.state.agent,
                                dynamic_agent,
                                enriched_text,
                                sid,
                                cancel_requested,
                            ),
                        ),
                    )
        else:
            # Honour the session's routing override. routing_mode "chat"
            # forces the chat path (no /chat prefix needed); "experts"
            # rejects chat/none classifications. Keep the override scoped
            # to this turn context so concurrent sessions do not mutate the
            # shared ClioAgent instance.
            routing_override = routing_mode
            from clio_agent.agent import routing_mode_override as _routing_override  # noqa: PLC0415

            with _routing_override(routing_override), _cancellation_checker(cancel_requested):
                with _tool_session_context(sid):
                    pred = await _await_turn_work(
                        _try_streamed_forward_compat(
                            app,
                            enriched_text,
                            sid,
                            _emit_chunk,
                            session_mode=getattr(sess, "mode", "chat"),
                            session_edit_mode=getattr(sess, "edit_mode", "diff"),
                            cancel_requested=cancel_requested,
                        )
                    )
                    if pred is None:
                        loop = asyncio.get_running_loop()
                        turn_context = contextvars.copy_context()
                        pred = await _await_turn_work(
                            loop.run_in_executor(
                                None,
                                lambda: turn_context.run(
                                    _agent_forward_compat,
                                    app.state.agent,
                                    enriched_text,
                                    sid,
                                    getattr(sess, "mode", "chat"),
                                    getattr(sess, "edit_mode", "diff"),
                                    cancel_requested,
                                ),
                            ),
                        )
        answer_text = getattr(pred, "answer", "")
        selected_agent = getattr(pred, "selected_expert", "") or ""
        rationale = getattr(pred, "routing_rationale", "")
        route_source = getattr(pred, "route_source", "") or ""
        route_reason = getattr(pred, "route_reason", "") or rationale
        if auto_routed_agent is not None:
            selected_agent = selected_agent or auto_routed_agent.id
            keyword_reason = f"Matched registered user agent {auto_routed_agent.id!r} by keyword."
            route_source = "user_agent_keyword"
            rationale = rationale or keyword_reason
            route_reason = keyword_reason
        pred_error_info = _coerce_error_info(getattr(pred, "error_info", None))
        if pred_error_info is not None:
            if pred_error_info.error == "cancelled":
                pred_error_info.details.setdefault("session_id", sid)
            error_info = pred_error_info
            if not error_info.details.get("partial", False):
                answer_text = ""
        # iowarp/clio-agent#25: data branch reports which execution
        # path it took ("fast" or "expert_loop"). Empty when not
        # populated by ClioAgent.forward (older code paths, non-data
        # branches not yet migrated).
        execution_path = getattr(pred, "execution_path", "") or ""
        tools_called = _extract_tools_called(pred)
        raw_handoffs = getattr(pred, "expert_handoffs", None) or []
        if isinstance(raw_handoffs, list):
            expert_handoffs = [dict(row) for row in raw_handoffs if isinstance(row, dict)]
        if (
            dynamic_agent_used is not None
            and dynamic_agent_used.source == "expert_pack"
            and expert_handoffs
        ):
            expert_handoffs = await _execute_delegated_experts(
                dynamic_agent_used,
                expert_handoffs,
                source_text=enriched_text,
            )
        # Drain the per-session observer ledger so direct-tool short-
        # circuits (HDF5/Parquet/fs experts that bypass ReAct) still
        # report tools_called on the assistant message metadata.
        ledger = getattr(app.state, "tool_call_ledger", None)
        if ledger is not None:
            observed = ledger.pop(sid, [])
            if observed and not tools_called:
                tools_called = observed
            elif observed:
                # Both populated. Keep the expert's richer row shape, but
                # upgrade matching rows with live observer timing/provenance
                # so metadata does not claim the same real call was post-hoc.
                observed_by_key = {_tool_call_event_key(o): o for o in observed}
                seen: set[tuple[str, str]] = set()
                for row in tools_called:
                    call_key = _tool_call_event_key(row)
                    seen.add(call_key)
                    live_row = observed_by_key.get(call_key)
                    if live_row is None:
                        continue
                    for field_name in (
                        "duration_ms",
                        "cached",
                        "telemetry_source",
                        "ok",
                        "error",
                    ):
                        if field_name in live_row:
                            row[field_name] = live_row[field_name]
                for o in observed:
                    if _tool_call_event_key(o) not in seen:
                        tools_called.append(o)
        # iowarp/clio-agent#17 — surface DSPy reasoning as a
        # `thinking` Part. ChainOfThought predictions expose
        # ``.reasoning`` (single string); ReAct exposes
        # ``.trajectory`` (step-by-step trace). Fall back to the
        # generic `_trace` Prediction wraps either of them in.
        thinking_text = (
            getattr(pred, "reasoning", "")
            or _format_react_trajectory(getattr(pred, "trajectory", None))
            or ""
        )
        # CLIO-BBBBBBBBBB24: cost + token rollup. Real DSPy
        # predictions don't always populate .tokens / .cost_usd
        # directly — pull from the per-turn UsageTracker first
        # (works across threads + streaming), then LM history.
        raw_tokens = getattr(pred, "tokens", None)
        if raw_tokens is not None:
            for key in turn_tokens:
                if isinstance(raw_tokens, dict):
                    v = raw_tokens.get(key, 0)
                else:
                    v = getattr(raw_tokens, key, 0)
                turn_tokens[key] = int(v or 0)
        else:
            # Diff the LM history slice for this turn first — captures
            # planner + expert + chat calls cleanly. Falls back to
            # ``last entry only`` for older code paths, then to a
            # character-based estimate when the upstream proxy
            # reports zero (some OpenAI-compatible proxies don't
            # populate usage on chunked replies).
            history_end = _snapshot_lm_history_index(app)
            history_made_calls = any(
                history_end.get(k, 0) > history_start.get(k, 0)
                for k in {*history_start.keys(), *history_end.keys()}
            )
            usage = _usage_from_history_slice(history_start, app)
            if not usage.get("output"):
                usage = _usage_from_dspy_history()
            for key in turn_tokens:
                turn_tokens[key] = int(usage.get(key, 0) or 0)
            turn_cost = float(usage.get("cost_usd", 0.0) or 0.0)
            # Char-based fallback only when the LM actually fired
            # this turn (history grew) but the upstream proxy
            # reported zero usage. Don't synthesize numbers when
            # there was no real call (e.g. unit tests with a fake
            # agent that bypasses dspy.LM entirely).
            if history_made_calls:
                if turn_tokens["output"] == 0 and answer_text:
                    turn_tokens["output"] = max(1, len(answer_text) // 4)
                if turn_tokens["input"] == 0 and enriched_text:
                    turn_tokens["input"] = max(1, len(enriched_text) // 4)
                if turn_cost == 0.0:
                    turn_cost = _estimate_cost_usd(
                        _current_lm_model_id(),
                        turn_tokens["input"],
                        turn_tokens["output"],
                    )
        if not turn_cost:
            turn_cost = float(getattr(pred, "cost_usd", 0.0) or 0.0)
        proposed_diffs = list(getattr(pred, "file_diffs", None) or [])
        nanoagents = list(getattr(pred, "nanoagents_spawned", None) or [])
        for req in getattr(pred, "permissions_requested", None) or []:
            src = (
                req
                if isinstance(req, dict)
                else {
                    "tool_call": getattr(req, "tool_call", {}),
                    "summary": getattr(req, "summary", ""),
                    "id": getattr(req, "id", ""),
                }
            )
            pid = src.get("id") or f"perm_{uuid.uuid4().hex[:12]}"
            row = {
                "id": pid,
                "session_id": sid,
                "tool_call": src.get("tool_call") or {},
                "summary": src.get("summary", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            }
            app.state.permissions[pid] = row
            bus.publish(
                Event(
                    type="permission.requested",
                    session_id=sid,
                    payload=row,
                )
            )
        if sid in app.state.cancel_flags:
            app.state.cancel_flags.discard(sid)
            error_info = _cancelled_error_info(
                sid,
                execution_cancellation="turn_boundary",
                executor_work_may_continue=False,
            )
            answer_text = ""
            tools_called = []
    except _TurnCancelled as exc:
        error_info = exc.error_info
        answer_text = ""
        tools_called = []
    except asyncio.CancelledError:
        error_info = _cancelled_error_info(
            sid,
            execution_cancellation="best_effort",
            executor_work_may_continue=True,
        )
        answer_text = ""
        tools_called = []
    except _StreamingOutputError as exc:
        original = exc.__cause__ or exc
        error_info = ErrorInfo(
            error="provider_error",
            message=str(exc),
            details={
                "original_error": type(original).__name__,
                "partial_output": bool(streamed_assistant_buffer),
                "stream_source": ("live" if streamed_assistant_buffer else "batch"),
            },
            recoverable=True,
        )
        answer_text = "".join(streamed_assistant_buffer)
        tools_called = []
    except _TurnTimedOut as exc:
        partial_output = bool(streamed_assistant_buffer)
        error_info = ErrorInfo(
            error="provider_timeout",
            message=f"agent turn exceeded {exc.timeout_s:g}s timeout",
            details={
                "session_id": sid,
                "timeout_s": exc.timeout_s,
                "partial_output": partial_output,
                "execution_cancellation": "best_effort",
                "executor_work_may_continue": True,
                "recovery_actions": [
                    "retry",
                    "increase_turn_timeout",
                    "reconfigure_provider",
                    "exit",
                ],
            },
            recoverable=True,
        )
        answer_text = "".join(streamed_assistant_buffer)
        tools_called = []
    except _UnsupportedSessionAgent as exc:
        selected_agent = exc.agent_id
        rationale = (
            "Session selected an agent that is registered but not executable "
            "by CLIO's current runtime."
        )
        error_info = ErrorInfo(
            error="not_implemented",
            message=(f"Session agent {exc.agent_id!r} cannot be executed yet."),
            details={
                "agent_id": exc.agent_id,
                "reason": exc.reason,
                "supported_agent_ids": sorted(
                    agent_id for agent_id in _EXECUTABLE_SESSION_AGENT_IDS if agent_id
                ),
                "unsupported_tools": exc.tools,
                "recovery_actions": [
                    "choose_builtin_agent",
                    "remove_custom_agent_tools",
                    "retry",
                    "exit",
                ],
            },
            recoverable=True,
        )
        answer_text = ""
        tools_called = []
    except _ContextFileAccessError as exc:
        error_info = exc.error_info
        answer_text = ""
        tools_called = []
    except Exception as exc:  # noqa: BLE001
        error_info = ErrorInfo(
            error="agent_error",
            message=f"agent.forward raised: {exc}",
            details={"original_error": type(exc).__name__},
            recoverable=True,
        )

    # Build assistant parts — routing_decision (v0.2) first when we
    # got a selected_agent, then optional thinking trace, then the
    # text answer, then any file_diffs.
    if (
        error_info is None
        and not answer_text
        and not thinking_text
        and not proposed_diffs
        and not nanoagents
    ):
        error_info = ErrorInfo(
            error="empty_response",
            message="Agent completed without user-visible output.",
            details={
                "session_id": sid,
                "routing_mode": getattr(sess, "routing_mode", "auto"),
                "selected_agent": selected_agent,
            },
            recoverable=True,
        )

    assistant_parts: list[Part] = []
    if selected_agent:
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="routing_decision",
                metadata={
                    k: v
                    for k, v in {
                        "route_source": route_source,
                        "route_reason": route_reason,
                    }.items()
                    if v
                },
                selected_agent=selected_agent,
                rationale=rationale,
                confidence=0.0,
                heuristic=False,
                execution_path=execution_path,
            )
        )
    for handoff in expert_handoffs:
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="expert_handoff",
                metadata=handoff,
                text=_expert_handoff_summary(handoff),
            )
        )
    if thinking_text:
        # iowarp/clio-agent#17: surface DSPy reasoning as a
        # thinking Part so the TUI can collapse + render it
        # gated on capabilities.thinking_blocks.
        assistant_parts.append(Part(id=_new_part_id(), type="thinking", text=thinking_text))
    if answer_text:
        assistant_parts.append(Part(id=_new_part_id(), type="text", text=answer_text))
    for row in proposed_diffs:
        if isinstance(row, dict):
            getf = row.get
        else:

            def getf(k, default=None, _r=row):
                return getattr(_r, k, default)

        path = getf("path", "") or ""
        udiff = getf("unified_diff", "") or ""
        new_content = getf("new_content", "") or ""
        edit_mode = getf("edit_mode", "") or ""
        lines_added = int(getf("lines_added", 0) or 0)
        lines_removed = int(getf("lines_removed", 0) or 0)
        if not path:
            continue
        # In "whole" mode the unified_diff may be empty by design;
        # the new_content carries the full replacement. Accept either
        # so the Part lands instead of being dropped.
        if not udiff and not new_content:
            continue
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="file_diff",
                path=path,
                unified_diff=udiff,
                new_content=new_content,
                status="pending",
                edit_mode=edit_mode,
                lines_added=lines_added,
                lines_removed=lines_removed,
            )
        )

    error_info = _enrich_cancellation_error_info(app, sid, error_info)
    cancelled_turn = error_info is not None and error_info.error == "cancelled"
    if cancelled_turn:
        app.state.cancel_flags.discard(sid)
        ledger = getattr(app.state, "tool_call_ledger", None)
        if ledger is not None:
            ledger.pop(sid, None)

    assistant_metadata: dict[str, Any] = {}
    if turn_agent_id:
        assistant_metadata["agent_override"] = {
            "requested_agent_id": turn_agent_id,
            "session_agent_id": _session_agent_id(sess),
            "effective_agent_id": selected_agent or turn_agent_id,
            "scope": "turn",
        }
    should_report_stream_provenance = bool(answer_text) or error_info is not None
    text_stream_source = (
        ("live" if streamed_assistant_part_id is not None else "batch")
        if should_report_stream_provenance
        else ""
    )
    if text_stream_source:
        assistant_metadata["stream_source"] = text_stream_source
    stream_fallback = _pop_stream_fallback(app, sid)
    if text_stream_source == "batch":
        if not stream_fallback:
            stream_fallback = _stream_fallback_payload("sync_execution_path")
        assistant_metadata["stream_fallback"] = stream_fallback
    if text_stream_source:
        for part in assistant_parts:
            if part.type != "text" or not part.text:
                continue
            part.metadata = {
                **part.metadata,
                "stream_source": text_stream_source,
            }
            if text_stream_source == "batch" and stream_fallback:
                part.metadata["stream_fallback"] = stream_fallback
    if tools_called:
        assistant_metadata["tools_called"] = tools_called
    if expert_handoffs:
        assistant_metadata["expert_handoffs"] = expert_handoffs
    if context_file_provenance["files"]:
        assistant_metadata["context_files"] = context_file_provenance
    if memory_search_metadata:
        assistant_metadata["memory_search"] = memory_search_metadata
    if agent_runtime:
        assistant_metadata["agent_runtime"] = agent_runtime
    if prompt_resolution:
        assistant_metadata["prompt_resolution"] = prompt_resolution
    # iowarp/clio-agent#6: when streaming actually emitted chunks,
    # reuse its message_id + part_id so the deltas + final
    # message line up. Otherwise mint a fresh id (existing path).
    asst_id = streamed_assistant_msg_id or _new_message_id("asst")
    if streamed_assistant_part_id is not None and answer_text:
        # Replace the routing/text/diff parts list's text part
        # with a stub carrying the streamed part_id, so the final
        # message references the same id the deltas used.
        for i, p in enumerate(assistant_parts):
            if p.type == "text":
                assistant_parts[i] = Part(
                    id=streamed_assistant_part_id,
                    type="text",
                    text=answer_text,
                    metadata=p.metadata,
                )
                break
    assistant_msg = Message(
        id=asst_id,
        session_id=sid,
        role="assistant",
        created_at=_iso_from_epoch(time.time()),
        updated_at=_iso_from_epoch(time.time()),
        parts=assistant_parts,
        tokens=Tokens(**turn_tokens),
        cost_usd=turn_cost,
        stop_reason="cancelled" if cancelled_turn else ("error" if error_info else "end_turn"),
        error_info=error_info,
        metadata=assistant_metadata,
    )
    _finalize_context_frame(
        app,
        sid,
        context_frame["id"],
        assistant_msg.id,
        "cancelled" if cancelled_turn else ("error" if error_info else "completed"),
        error_info=error_info,
    )

    # Index file_diff parts so /diffs/apply + /diffs/reject find them.
    bucket = app.state.pending_diffs.setdefault(sid, [])
    for p in assistant_parts:
        if p.type != "file_diff":
            continue
        write_content = (
            p.new_content if p.new_content or p.edit_mode in {"whole", "patch"} else None
        )
        bucket.append(
            {
                "path": p.path,
                "unified_diff": p.unified_diff,
                "new_content": write_content,
                "status": "pending",
                "part_id": p.id,
                "message_id": assistant_msg.id,
            }
        )

    # Materialise nanoagent spawns + publish their lifecycle events.
    for spawn in nanoagents:
        get = (
            spawn.get
            if isinstance(spawn, dict)
            else (lambda k, default=None, _s=spawn: getattr(_s, k, default))
        )
        agent_id = get("agent_id") or get("agent") or "nanoagent"
        spawn_input = get("input") or {}
        answer = get("answer") or ""
        tools_called = get("tools_called") or get("tools") or []
        subsess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=f"{agent_id} subagent",
            parent_session_id=sid,
            agent={"id": str(agent_id), "mode": "subagent"},
            metadata={
                "session_type": "nanoagent",
                "agent_id": str(agent_id),
                "parent_session_id": sid,
                "spawned_by_message_id": assistant_msg.id,
                "spawned_by_agent": selected_agent,
                "tool_count": len(tools_called) if isinstance(tools_called, list) else 0,
            },
        )
        sub_now = time.time()
        sub_user = Message(
            id=_new_message_id("user"),
            session_id=subsess.id,
            role="user",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[
                Part(
                    id=_new_part_id(),
                    type="text",
                    text=_format_subagent_input(spawn_input),
                )
            ],
            metadata={
                "subagent_input": spawn_input,
                "parent_session_id": sid,
                "spawned_by_message_id": assistant_msg.id,
            },
        )
        sub_asst = Message(
            id=_new_message_id("asst"),
            session_id=subsess.id,
            role="assistant",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[Part(id=_new_part_id(), type="text", text=answer)] if answer else [],
            stop_reason="end_turn",
            metadata={"tools_called": tools_called} if tools_called else {},
        )
        _extend_session_messages(app, subsess.id, [sub_user, sub_asst])
        app.state.sessions.update(subsess.id, message_count=2, status="idle")
        bus.publish(
            Event(
                type="subagent.started",
                session_id=sid,
                payload={
                    "parent_session_id": sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "spawned_by_message_id": assistant_msg.id,
                },
            )
        )
        bus.publish(
            Event(
                type="subagent.completed",
                session_id=sid,
                payload={
                    "parent_session_id": sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                    "tokens": get("tokens") or {},
                    "cost_usd": float(get("cost_usd", 0.0) or 0.0),
                },
            )
        )

    # message.created for the assistant message (empty body — parts
    # arrive via subsequent message.part.added/delta events).
    # When real streaming already fired the message.created +
    # message.part.added + N deltas (#6), skip re-issuing them so we
    # don't duplicate.
    if streamed_assistant_msg_id is None:
        bus.publish(
            Event(
                type="message.created",
                session_id=sid,
                payload=Message(
                    id=assistant_msg.id,
                    session_id=sid,
                    role="assistant",
                    created_at=assistant_msg.created_at,
                    updated_at=assistant_msg.updated_at,
                    parts=[],
                ).model_dump(exclude_none=True),
            )
        )
    # Stream live text parts via message.part.delta. When a turn only has
    # post-hoc text, publish the completed text as a normal part instead
    # of chunking it into synthetic deltas that could be mistaken for live
    # provider tokens.
    for part in assistant_parts:
        if part.type == "text" and part.text:
            if part.id == streamed_assistant_part_id:
                # Real streaming already pumped deltas — but those
                # carry raw LM output that includes ChatAdapter format
                # markers ([[ ## answer ## ]] etc). The final ``part.text``
                # is the parsed clean answer; ship it on the completed
                # event so the TUI can replace the buffered text.
                bus.publish(
                    Event(
                        type="message.part.completed",
                        session_id=sid,
                        payload={
                            "message_id": assistant_msg.id,
                            "part_id": part.id,
                            "stream_source": "live",
                            "final_text": part.text,
                        },
                    )
                )
                continue
            delivered = part.model_copy(deep=True)
            delivered.metadata = {
                **delivered.metadata,
                "stream_source": "batch",
            }
            if stream_fallback:
                delivered.metadata["stream_fallback"] = stream_fallback
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "message_id": assistant_msg.id,
                        "part": delivered.model_dump(exclude_none=True),
                    },
                )
            )
            bus.publish(
                Event(
                    type="message.part.completed",
                    session_id=sid,
                    payload={
                        "message_id": assistant_msg.id,
                        "part_id": part.id,
                        "stream_source": "batch",
                        "stream_fallback": stream_fallback,
                        "final_text": part.text,
                    },
                )
            )
        else:
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "message_id": assistant_msg.id,
                        "part": part.model_dump(exclude_none=True),
                    },
                )
            )
    # Tool lifecycle events are only emitted by the live observer at the
    # execution boundary. Prediction.tools_called remains summary metadata;
    # do not reconstruct started/completed events after the turn, because
    # that makes post-hoc facts look like live tool timing.
    completed_payload: dict[str, Any] = {
        "message_id": assistant_msg.id,
        "stop_reason": "cancelled" if cancelled_turn else ("error" if error_info else "end_turn"),
        "tokens": dict(turn_tokens),
        "cost_usd": turn_cost,
    }
    if error_info is not None:
        completed_payload["error_info"] = error_info.model_dump(exclude_none=True)
    if assistant_metadata:
        completed_payload["metadata"] = assistant_metadata
    bus.publish(
        Event(
            type="message.completed",
            session_id=sid,
            payload=completed_payload,
        )
    )

    # Persist + settle.
    final_status = "cancelled" if cancelled_turn else ("error" if error_info else "idle")
    retry_status = "cancelled" if cancelled_turn else ("failed" if error_info else "completed")
    _append_session_message(app, sid, assistant_msg)
    _update_retry_attempt(
        retry_status,
        metadata_patch={
            "executed_user_message_id": user_msg.id,
            "assistant_message_id": assistant_msg.id,
            "stop_reason": completed_payload["stop_reason"],
        },
    )
    app.state.sessions.update(
        sid,
        status=final_status,
        message_count=sess.message_count + 2,
        add_tokens_input=turn_tokens["input"],
        add_tokens_output=turn_tokens["output"],
        add_cost_usd=turn_cost,
    )
    cancellation_status: dict[str, Any] = {}
    if cancelled_turn and error_info is not None:
        cancellation_status = {
            "execution_cancellation": error_info.details.get("execution_cancellation"),
            "executor_work_may_continue": error_info.details.get("executor_work_may_continue"),
            "cancellation_attempt": error_info.details.get("cancellation_attempt", {}),
        }
    bus.publish(
        Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": final_status,
                "prev_status": "running",
                **cancellation_status,
            },
        )
    )
    # iowarp/clio-agent#20: post_message hook runs AFTER persistence
    # so user audit code sees the settled assistant + can ship to
    # external systems. Errors are swallowed (post_* contract).
    try:
        from clio_agent.runtime.hooks import fire as _fire_hook

        _fire_hook(
            "post_message",
            sid,
            assistant_msg.model_dump(exclude_none=True),
        )
    except Exception:  # noqa: BLE001
        pass
    if not (
        cancelled_turn
        and error_info is not None
        and error_info.details.get("execution_cancellation") == "best_effort"
    ):
        if app.state.cancel_events.get(sid) is turn_cancel_event:
            app.state.cancel_events.pop(sid, None)


def _current_lm_model_id() -> str:
    """Best-effort: which model is dspy.settings.lm bound to."""
    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return ""
    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    return getattr(lm, "model", "") if lm else ""


def _estimate_context_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _message_text_for_frame(message: "Message") -> str:
    chunks: list[str] = []
    for part in getattr(message, "parts", []) or []:
        text = getattr(part, "text", "") or ""
        if text:
            chunks.append(text)
        for attr in ("path", "unified_diff", "new_content"):
            value = getattr(part, attr, "") or ""
            if value:
                chunks.append(str(value))
    return "\n".join(chunks)


def _record_context_frame(
    app: "FastAPI",
    sid: str,
    sess: Any,
    user_msg: "Message",
    *,
    user_text: str,
    enriched_text: str,
    context_error: Optional[ErrorInfo],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    visible_messages = list(app.state.messages.get(sid, []))
    items: list[dict[str, Any]] = []
    token_total = 0
    for msg in visible_messages:
        msg_text = _message_text_for_frame(msg)
        tokens = (
            int(getattr(msg.tokens, "input", 0) or 0)
            + int(getattr(msg.tokens, "output", 0) or 0)
            + int(getattr(msg.tokens, "cache_read", 0) or 0)
            + int(getattr(msg.tokens, "cache_write", 0) or 0)
        )
        if tokens <= 0:
            tokens = _estimate_context_tokens(msg_text)
        token_total += tokens
        items.append(
            {
                "kind": "message",
                "source_id": msg.id,
                "role": msg.role,
                "included": True,
                "reason": "visible_transcript",
                "tokens_estimated": tokens,
                "metadata": {
                    "synthetic": (msg.metadata or {}).get("synthetic", ""),
                    "part_count": len(msg.parts),
                },
            }
        )

    for row in (app.state.context_files.get(sid, {}) or {}).values():
        path = str(row.get("resolved_path") or row.get("path") or "")
        display_path = str(row.get("display_path") or row.get("path") or path)
        try:
            raw_size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            raw_size = 0
        tokens = max(0, min(max(raw_size, 0), _CTX_MAX_BYTES) // 4)
        token_total += tokens
        items.append(
            {
                "kind": "context_file",
                "source_id": display_path,
                "path": path,
                "display_path": display_path,
                "included": context_error is None,
                "reason": "attached_context_file" if context_error is None else "context_error",
                "tokens_estimated": tokens,
                "metadata": {
                    "mode": row.get("mode", ""),
                    "source": row.get("source", ""),
                    "workspace_id": row.get("workspace_id", ""),
                    "language": row.get("language", ""),
                },
            }
        )

    enriched_delta = max(0, len(enriched_text) - len(user_text))
    agent_ref = getattr(sess, "agent", {}) or {}
    frame = {
        "id": _new_context_frame_id(),
        "session_id": sid,
        "turn_id": user_msg.id,
        "user_message_id": user_msg.id,
        "assistant_message_id": "",
        "created_at": now,
        "updated_at": now,
        "status": "context_error" if context_error is not None else "assembled",
        "model": _active_lm_model_ref(app),
        "agent": {
            "id": _session_agent_id(sess),
            "mode": agent_ref.get("mode", "") if isinstance(agent_ref, dict) else "",
            "routing_mode": getattr(sess, "routing_mode", "auto"),
            "session_mode": getattr(sess, "mode", "chat"),
            "edit_mode": getattr(sess, "edit_mode", "diff"),
        },
        "prompt": {
            "profile": (getattr(sess, "metadata", {}) or {}).get("prompt_profile", ""),
            "source": "runtime_default",
        },
        "items": items,
        "tokens_estimated": token_total,
        "metadata": {
            "retained_context_source": "visible_gact_transcript",
            "token_estimate": "message_tokens_or_chars_div_4",
            "context_file_injected_chars": enriched_delta,
            "context_error": context_error.model_dump(exclude_none=True)
            if context_error is not None
            else {},
        },
    }
    app.state.context_frames.setdefault(sid, []).append(frame)
    app.state.bus.publish(Event(type="context.frame.created", session_id=sid, payload=frame))
    return frame


def _finalize_context_frame(
    app: "FastAPI",
    sid: str,
    frame_id: str,
    assistant_message_id: str,
    status: str,
    *,
    error_info: Optional[ErrorInfo],
) -> None:
    frames = app.state.context_frames.get(sid, [])
    for frame in frames:
        if frame.get("id") != frame_id:
            continue
        frame["assistant_message_id"] = assistant_message_id
        frame["status"] = status
        frame["updated_at"] = datetime.now(timezone.utc).isoformat()
        if error_info is not None:
            frame.setdefault("metadata", {})["turn_error"] = error_info.model_dump(
                exclude_none=True
            )
        app.state.bus.publish(
            Event(type="context.frame.completed", session_id=sid, payload=frame)
        )
        break


def _all_known_lms(app: "FastAPI") -> list[Any]:
    """Return every LM instance the running agent might call —
    ``dspy.settings.lm`` plus the agent's ``_planner_lm`` and any
    expert-bound LMs. Lets the turn handler diff history across
    all of them so planner + expert + chat token counts roll up."""

    lms: list[Any] = []
    try:
        import dspy  # noqa: PLC0415

        main = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
        if main is not None:
            lms.append(main)
    except Exception:  # pragma: no cover
        pass
    agent = getattr(getattr(app, "state", None), "agent", None)
    for attr in ("_planner_lm", "_router_lm", "router_lm", "_expert_lm"):
        side = getattr(agent, attr, None) if agent is not None else None
        if side is not None and side not in lms:
            lms.append(side)
    return lms


def _snapshot_lm_history_index(app: Optional["FastAPI"] = None) -> dict[int, int]:
    """Return current ``len(lm.history)`` for every known LM,
    keyed by ``id(lm)`` so the diff side can find them again
    even if the agent rebinds attributes mid-turn."""

    if app is None:
        try:
            import dspy  # noqa: PLC0415
        except Exception:  # pragma: no cover
            return {}
        lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
        return {id(lm): len(getattr(lm, "history", None) or [])} if lm else {}
    snapshot: dict[int, int] = {}
    for lm in _all_known_lms(app):
        history = getattr(lm, "history", None) or []
        snapshot[id(lm)] = len(history)
    return snapshot


def _usage_from_history_slice(start: Any, app: Optional["FastAPI"] = None) -> dict[str, Any]:
    """Sum usage from each known LM's ``history[start:]`` — every
    call this turn made across planner + experts + chat. Accepts
    either a ``dict[id(lm) -> int]`` snapshot (preferred) or a
    legacy single int for backwards compat with single-LM callers.
    """

    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return {}
    if app is not None:
        lms = _all_known_lms(app)
    else:
        lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
        lms = [lm] if lm else []
    if not lms:
        return {}
    if isinstance(start, int):
        # Legacy single-int callers — apply to main LM only.
        snap = {id(lms[0]): start}
    else:
        snap = start
    input_tok = output_tok = cache_read = cache_write = 0
    raw_cost = 0.0
    last_model = ""
    for lm in lms:
        start_idx = snap.get(id(lm), 0)
        history = getattr(lm, "history", None) or []
        for entry in history[start_idx:]:
            if not isinstance(entry, dict):
                continue
            usage = entry.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            input_tok += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tok += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            cache_write += int(usage.get("cache_creation_input_tokens") or 0)
            raw_cost += float(usage.get("cost_usd") or usage.get("total_cost") or 0.0)
            last_model = entry.get("model") or last_model
    if raw_cost == 0.0:
        raw_cost = _estimate_cost_usd(last_model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


def _usage_from_history_slice_legacy(start: int) -> dict[str, Any]:
    """Single-LM history diff retained for tests that don't pass
    an app. Walks dspy.settings.lm only."""

    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return {}
    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    if lm is None:
        return {}
    history = getattr(lm, "history", None) or []
    if start >= len(history):
        return {}
    input_tok = 0
    output_tok = 0
    cache_read = 0
    cache_write = 0
    raw_cost = 0.0
    last_model = ""
    for entry in history[start:]:
        if not isinstance(entry, dict):
            continue
        usage = entry.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        input_tok += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tok += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cache_read += int(usage.get("cache_read_input_tokens") or 0)
        cache_write += int(usage.get("cache_creation_input_tokens") or 0)
        raw_cost += float(usage.get("cost_usd") or usage.get("total_cost") or 0.0)
        last_model = entry.get("model") or last_model
    if raw_cost == 0.0:
        raw_cost = _estimate_cost_usd(last_model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


def _usage_from_tracker(tracker: Any) -> dict[str, Any]:
    """Sum usage from a per-turn ``UsageTracker`` (preferred path).

    The tracker collects per-call usage as litellm/dspy hits the LM,
    surviving the executor-thread + streaming hops that strand
    ``dspy.LM.history``. Returns ``{}`` when the tracker is absent
    or empty so the caller falls back to history scraping.
    """

    if tracker is None:
        return {}
    try:
        totals = tracker.get_total_tokens()
    except Exception:  # noqa: BLE001
        return {}
    if not totals:
        return {}
    input_tok = 0
    output_tok = 0
    cache_read = 0
    cache_write = 0
    raw_cost = 0.0
    last_model = ""
    for model, entry in totals.items():
        last_model = model
        input_tok += int(entry.get("prompt_tokens") or entry.get("input_tokens") or 0)
        output_tok += int(entry.get("completion_tokens") or entry.get("output_tokens") or 0)
        cache_read += int(entry.get("cache_read_input_tokens") or 0)
        cache_write += int(entry.get("cache_creation_input_tokens") or 0)
        raw_cost += float(entry.get("cost_usd") or entry.get("total_cost") or 0.0)
    if raw_cost == 0.0:
        raw_cost = _estimate_cost_usd(last_model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


def _usage_from_dspy_history() -> dict[str, Any]:
    """Reach into DSPy's currently-configured LM and pull the most
    recent call's usage block. Returns ``{}`` whenever DSPy isn't
    importable, no LM is configured, or the history is empty —
    callers default to zeros.

    Best-effort. DSPy's history shape changes between minor versions;
    we accept any dict-shaped record under ``lm.history[-1]`` whose
    ``usage`` (or ``response.usage``) carries the OpenAI-style keys
    we already use on the wire.
    """

    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover - dspy not present
        return {}

    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    if lm is None:
        return {}
    history = getattr(lm, "history", None)
    if not history:
        return {}
    last = history[-1]
    usage = last.get("usage") if isinstance(last, dict) else getattr(last, "usage", None)
    if usage is None and isinstance(last, dict):
        resp = last.get("response", {}) or {}
        usage = resp.get("usage", {}) if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return {}
    input_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    raw_cost = float(usage.get("cost_usd") or usage.get("total_cost") or 0.0)
    # iowarp/clio-agent#8: some OpenAI-compatible proxies don't pass
    # cost_usd through, so the upstream usage dict reports zero. Fall
    # back to a per-token price table keyed by the LM's model id when
    # raw_cost == 0.
    if raw_cost == 0.0:
        model = ""
        if isinstance(last, dict):
            model = last.get("model") or last.get("response", {}).get("model", "") or ""
        else:
            model = getattr(last, "model", "") or ""
        raw_cost = _estimate_cost_usd(model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


# iowarp/clio-agent#8: per-million-token prices (USD) for models we
# expect to see through our presets. Best-effort — the LM provider
# is the source of truth when it actually reports cost; this kicks
# in only when the upstream usage dict has zero. Keys match the
# substrings we look for in the reported model id (case-insensitive).
_PRICE_TABLE_PER_M: dict[str, tuple[float, float]] = {
    # (input $/M tokens, output $/M tokens) as of model-card pricing.
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-opus": (15.0, 75.0),
    # OpenRouter free tier — by definition $0.
    ":free": (0.0, 0.0),
    # OpenAI defaults if someone wires direct.
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
}


def _estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Best-effort cost estimate when the LM doesn't report one.

    Substring-matches the model id against ``_PRICE_TABLE_PER_M``;
    returns 0.0 when nothing matches (no false-precision number).
    """

    if not model_id:
        return 0.0
    needle = model_id.lower()
    match: Optional[tuple[float, float]] = None
    for key, prices in _PRICE_TABLE_PER_M.items():
        if key in needle:
            match = prices
            break
    if match is None:
        return 0.0
    input_price, output_price = match
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


# iowarp/clio-agent#7: tools the gate treats as destructive. Anything
# matching one of these substrings triggers a permission_requested
# event + blocks the bridge thread until the user resolves it.
_DESTRUCTIVE_TOOL_SUBSTRINGS: tuple[str, ...] = (
    "delete",
    "remove",
    "rm_",
    "drop",
    "destroy",
    "exec",
    "shell",
    "write",
)
_PERMISSION_POLICY_SCOPES = {"session", "workspace"}
_PERMISSION_POLICY_ACTIONS = {"allow", "allow_session", "allow_workspace", "deny", "ask"}


def _is_destructive(tool_name: str) -> bool:
    n = tool_name.lower()
    return any(needle in n for needle in _DESTRUCTIVE_TOOL_SUBSTRINGS)


def _is_safe_shell_diagnostic(tool_name: str, args: Mapping[str, Any]) -> bool:
    """Return whether a shell_bash call is a read-only local diagnostic."""

    if tool_name != "shell_bash":
        return False
    command = str(args.get("command") or "").strip().lower()
    command = re.sub(r"\s+", " ", command)
    return command in {
        "date",
        "get-date",
        "pwd",
        "whoami",
        "hostname",
    }


def _permission_path_from_args(args: Mapping[str, Any]) -> str:
    for key in ("filepath", "path", "output_path", "target_path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _policy_action_for_tool(
    app: "FastAPI",
    *,
    session_id: str,
    session: Any | None,
    tool_name: str,
    args: Mapping[str, Any],
) -> str:
    """Return the first matching permission policy action.

    The `/v1/policies` endpoint is user-facing configuration, so storing
    policies without enforcing them is a silent safety bypass. Matching is
    intentionally small and predictable: scope, tool glob, optional path glob,
    then the policy action.
    """

    policies = getattr(app.state, "permission_policies", [])
    if not isinstance(policies, list):
        return ""
    path = _permission_path_from_args(args)
    workspace_id = getattr(session, "workspace_id", "") if session is not None else ""
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        scope = str(policy.get("scope") or "").lower()
        scope_id = str(policy.get("scope_id") or "")
        if scope == "session":
            if scope_id and scope_id != session_id:
                continue
        elif scope == "workspace":
            if scope_id and scope_id != workspace_id:
                continue
        else:
            continue

        tool_pattern = str(policy.get("tool_name_pattern") or "*")
        if not fnmatch.fnmatchcase(tool_name, tool_pattern):
            continue

        path_pattern = str(policy.get("path_pattern") or "")
        if path_pattern:
            candidates = [path]
            if path:
                try:
                    candidates.append(str(Path(path).resolve(strict=False)))
                except OSError:
                    pass
            if not any(fnmatch.fnmatchcase(candidate, path_pattern) for candidate in candidates):
                continue

        action = str(policy.get("action") or "").lower()
        if action in {"allow", "allow_session", "allow_workspace", "deny", "ask"}:
            return action
    return ""


def _validate_permission_policies(
    policies: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and normalize `/v1/policies` rows.

    Invalid permission policies are a safety bug: silently dropping or storing
    a typoed deny rule can make a user believe a destructive action is blocked
    when it is not. Return every validation error so the caller can reject the
    whole update atomically.
    """

    clean: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, raw_policy in enumerate(policies):
        if not isinstance(raw_policy, dict):
            errors.append(
                {
                    "index": index,
                    "field": "policy",
                    "message": "policy must be an object",
                }
            )
            continue

        policy = dict(raw_policy)
        scope_raw = policy.get("scope")
        action_raw = policy.get("action")
        scope = scope_raw.strip().lower() if isinstance(scope_raw, str) else ""
        action = action_raw.strip().lower() if isinstance(action_raw, str) else ""
        policy_has_errors = False

        if scope not in _PERMISSION_POLICY_SCOPES:
            policy_has_errors = True
            errors.append(
                {
                    "index": index,
                    "field": "scope",
                    "message": "scope must be one of session, workspace",
                }
            )
        if action not in _PERMISSION_POLICY_ACTIONS:
            policy_has_errors = True
            errors.append(
                {
                    "index": index,
                    "field": "action",
                    "message": (
                        "action must be one of allow, allow_session, allow_workspace, deny, ask"
                    ),
                }
            )

        for field in ("scope_id", "tool_name_pattern", "path_pattern"):
            value = policy.get(field)
            if value is not None and not isinstance(value, str):
                policy_has_errors = True
                errors.append(
                    {
                        "index": index,
                        "field": field,
                        "message": f"{field} must be a string when present",
                    }
                )

        if policy_has_errors:
            continue

        policy["scope"] = scope
        policy["action"] = action
        clean.append(policy)
    return clean, errors


def _load_permission_policies(path: Path | None) -> list[dict[str, Any]]:
    """Load persisted permission policies, ignoring invalid rows."""

    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("policies", []) if isinstance(data, Mapping) else []
    if not isinstance(raw, list):
        return []
    clean, _errors = _validate_permission_policies(raw)
    return clean


def _flush_permission_policies(app: "FastAPI") -> None:
    """Persist the current permission policy list, if configured."""

    path = getattr(app.state, "permission_policies_path", None)
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"policies": app.state.permission_policies}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _record_resolved_permission(
    app: "FastAPI",
    *,
    session_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    status: str,
    action: str,
    summary: str,
    reason: str,
) -> str:
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    row = {
        "id": pid,
        "session_id": session_id,
        "tool_call": {
            "tool_name": tool_name,
            "input": dict(args),
        },
        "summary": summary,
        "created_at": now_iso,
        "status": status,
        "action": action,
        "resolved_at": now_iso,
        "reason": reason,
    }
    if hasattr(app.state, "permissions"):
        app.state.permissions[pid] = row
    if hasattr(app.state, "bus"):
        app.state.bus.publish(
            Event(
                type="permission.resolved",
                session_id=session_id,
                payload={
                    "permission_id": pid,
                    "action": action,
                    "session_id": session_id,
                    "reason": reason,
                },
            )
        )
    return pid


def _append_permission_policy_from_resolution(
    app: "FastAPI",
    *,
    row: Mapping[str, Any],
    action: str,
) -> dict[str, Any] | None:
    """Persist allow_session/allow_workspace decisions as policy rules."""

    if action not in {"allow_session", "allow_workspace"}:
        return None
    session_id = str(row.get("session_id") or "")
    raw_tool_call = row.get("tool_call")
    tool_call: Mapping[str, Any] = raw_tool_call if isinstance(raw_tool_call, Mapping) else {}
    tool_name = str(tool_call.get("tool_name") or "*")
    raw_args = tool_call.get("input")
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}
    session = app.state.sessions.get(session_id) if session_id else None
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    policy = {
        "scope": "session" if action == "allow_session" else "workspace",
        "scope_id": session_id if action == "allow_session" else workspace_id,
        "tool_name_pattern": tool_name,
        "action": "allow",
        "created_from_permission_id": str(row.get("id") or ""),
    }
    path = _permission_path_from_args(args)
    if path:
        policy["path_pattern"] = path
    app.state.permission_policies.append(policy)
    return policy


def _direct_permission_denied(
    *,
    tool_name: str,
    args: Mapping[str, Any],
    summary: str,
) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="permission_error",
                message=f"{summary} blocked by permission policy",
                details={
                    "tool_name": tool_name,
                    "input": dict(args),
                    "reason": "policy_deny",
                    "recovery_actions": ["change_policy", "retry", "exit"],
                },
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def _guard_direct_destructive_action(
    app: "FastAPI",
    *,
    session_id: str = "",
    workspace_id: str = "",
    tool_name: str,
    args: Mapping[str, Any],
    summary: str,
    reason: str,
) -> None:
    """Apply permission policy/audit semantics to direct GACT DELETE actions.

    These routes are already explicit user actions, so there is no extra
    interactive prompt. Policies can still deny before mutation, and all
    allowed direct destructive actions land in `/v1/permissions` as resolved
    audit rows.
    """

    session = app.state.sessions.get(session_id) if session_id else None
    if session is None and workspace_id:
        session = SimpleNamespace(workspace_id=workspace_id)
    policy_action = _policy_action_for_tool(
        app,
        session_id=session_id,
        session=session,
        tool_name=tool_name,
        args=args,
    )
    if policy_action == "deny":
        _record_resolved_permission(
            app,
            session_id=session_id,
            tool_name=tool_name,
            args=args,
            status="auto_denied",
            action="deny",
            summary=f"{summary} blocked by permission policy",
            reason="policy_deny",
        )
        raise _direct_permission_denied(tool_name=tool_name, args=args, summary=summary)
    _record_resolved_permission(
        app,
        session_id=session_id,
        tool_name=tool_name,
        args=args,
        status="auto_approved",
        action="allow",
        summary=summary,
        reason="policy_allow"
        if policy_action in {"allow", "allow_session", "allow_workspace"}
        else reason,
    )


def _make_permission_gate(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.permission_gate.

    Non-destructive tools fast-allow. Destructive tools register a
    permission row, publish permission.requested into the EventBus,
    block on a threading.Event with a generous timeout, and return
    "allow" / "deny" based on the user's resolution. Timeouts default
    to deny — fail-safe.
    """

    DEFAULT_TIMEOUT_S = 120.0

    def gate(name: str, args: Mapping[str, Any]) -> str:
        # iowarp/clio-agent#20: user-defined pre_tool hook can veto
        # the call by raising PermissionError. Returns ignored;
        # only the raise/no-raise distinction matters.
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _fire_hook("pre_tool", name, dict(args))
        except PermissionError:
            return "deny"
        if not _is_destructive(name):
            return "allow"
        # Prefer the session currently driving the turn. Recency is
        # only a fallback for truly out-of-band tool calls.
        sid, current = _resolve_tool_session(app)
        if current is not None:
            # iowarp/clio-agent — plan_mode + architect mode reject
            # destructive tool calls without prompting. Read-only
            # contract is hard, not advisory.
            if current.mode in {"plan", "architect"}:
                row = {
                    "id": f"perm_{uuid.uuid4().hex[:12]}",
                    "session_id": sid,
                    "tool_call": {
                        "tool_name": name,
                        "input": dict(args),
                    },
                    "summary": (
                        f"destructive tool {name!r} blocked by session.mode={current.mode!r}"
                    ),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "auto_denied",
                    "action": "deny",
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                app.state.permissions[row["id"]] = row
                app.state.bus.publish(
                    Event(
                        type="permission.resolved",
                        session_id=sid,
                        payload={
                            "permission_id": row["id"],
                            "action": "deny",
                            "session_id": sid,
                            "reason": "session_mode_readonly",
                        },
                    )
                )
                return "deny"
        policy_action = _policy_action_for_tool(
            app,
            session_id=sid,
            session=current,
            tool_name=name,
            args=args,
        )
        if policy_action == "deny":
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_denied",
                action="deny",
                summary=f"destructive tool {name!r} blocked by permission policy",
                reason="policy_deny",
            )
            return "deny"
        if policy_action in {"allow", "allow_session", "allow_workspace"}:
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_approved",
                action="allow",
                summary=f"destructive tool {name!r} allowed by permission policy",
                reason=f"policy_{policy_action}",
            )
            return "allow"
        if _is_safe_shell_diagnostic(name, args):
            return "allow"
        if not sid:
            return "deny"
        pid = f"perm_{uuid.uuid4().hex[:12]}"
        evt = threading.Event()
        row = {
            "id": pid,
            "session_id": sid,
            "tool_call": {
                "tool_name": name,
                "input": dict(args),
            },
            "summary": f"destructive tool call: {name}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        app.state.permissions[pid] = row
        app.state.permission_events[pid] = evt
        app.state.bus.publish(
            Event(
                type="permission.requested",
                session_id=sid,
                payload=row,
            )
        )
        # Block the bridge thread until POST /v1/permissions/{pid}
        # sets the event (or we time out).
        if not evt.wait(timeout=DEFAULT_TIMEOUT_S):
            row["status"] = "timeout"
            return "deny"
        action = row.get("action", "deny")
        if action in {"allow", "allow_session", "allow_workspace"}:
            return "allow"
        return "deny"

    return gate


def _make_cancellation_checker(app: "FastAPI"):
    """Build a tool-executor cancellation checker for the active GACT session."""

    def check() -> bool:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return False
        event = app.state.cancel_events.get(sid)
        if event is not None and event.is_set():
            return True
        return sid in app.state.cancel_flags

    return check


def _make_tool_observer(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.tool_observer.

    Publishes tool.call.started / tool.call.completed events into
    the EventBus, attaching to the active turn session when present
    and falling back to recency only for out-of-band calls. Also
    appends each completed call into ``app.state.tool_call_ledger[sid]`` so the
    turn handler can attach a per-turn ``tools_called`` list to the
    assistant message metadata even when the underlying expert
    didn't populate ``pred.tools_called`` itself (e.g. the
    deterministic short-circuit paths).
    """

    def observe(
        name: str,
        args: Mapping[str, Any],
        phase: Optional[str],
        error: Optional[str],
    ) -> None:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return
        if phase == "started":
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            # Stash the per-thread call_id so the completion event
            # uses the same id. Threading-locals works for
            # MCPToolBridge's worker thread.
            _OBSERVER_CALL_IDS.value = call_id
            # Stamp the start time so completion can compute duration.
            _OBSERVER_CALL_T0.value = time.time()
            app.state.bus.publish(
                Event(
                    type="tool.call.started",
                    session_id=sid,
                    payload={
                        "call_id": call_id,
                        "tool": name,
                        "args": dict(args),
                        "telemetry_source": "live_observer",
                    },
                )
            )
        elif phase == "completed":
            call_id = getattr(_OBSERVER_CALL_IDS, "value", "") or ""
            t0 = getattr(_OBSERVER_CALL_T0, "value", None)
            duration_ms = (time.time() - t0) * 1000 if t0 else 0.0
            cancel_event = app.state.cancel_events.get(sid)
            completed_after_cancel = sid in app.state.cancel_flags or (
                cancel_event is not None and cancel_event.is_set()
            )
            completion_error = error
            cancellation_metadata: dict[str, Any] = {}
            if completed_after_cancel:
                completion_error = (
                    completion_error or "tool call completed after session cancellation"
                )
                cancellation_metadata = {
                    "execution_cancellation": "best_effort",
                    "executor_work_may_continue": True,
                }
            ok = completion_error is None
            payload = {
                "call_id": call_id,
                "tool": name,
                "ok": ok,
                "duration_ms": duration_ms,
                "cached": False,
                "telemetry_source": "live_observer",
                **({"error": completion_error} if completion_error else {}),
                **cancellation_metadata,
            }
            app.state.bus.publish(
                Event(
                    type="tool.call.completed",
                    session_id=sid,
                    payload=payload,
                )
            )
            # Append to the per-session ledger so the turn handler
            # finds it post-forward and attaches to the assistant
            # message metadata.
            ledger = getattr(app.state, "tool_call_ledger", None)
            if ledger is not None and not completed_after_cancel:
                ledger.setdefault(sid, []).append(
                    {
                        "name": name,
                        "args": dict(args),
                        "ok": ok,
                        "duration_ms": duration_ms,
                        "cached": False,
                        "telemetry_source": "live_observer",
                        **({"error": completion_error} if completion_error else {}),
                        **cancellation_metadata,
                    }
                )

    return observe


_OBSERVER_CALL_T0 = threading.local()


def _agent_forward_compat(
    agent: Any,
    question: str,
    session_id: str,
    session_mode: str,
    session_edit_mode: str,
    cancel_requested: Any | None = None,
) -> Any:
    """Call agent.forward, threading session_mode + session_edit_mode
    when the agent accepts them, falling back to the legacy
    ``(question, session_id)`` signature for fakes / older builds.

    Lets us add new optional kwargs to the contract without breaking
    every test fixture that hand-rolled a minimal forward signature.
    """

    try:
        return agent.forward(
            question,
            session_id=session_id,
            session_mode=session_mode,
            session_edit_mode=session_edit_mode,
            cancel_requested=cancel_requested,
        )
    except TypeError:
        try:
            return agent.forward(
                question,
                session_id=session_id,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
            )
        except TypeError:
            return agent.forward(question, session_id=session_id)


async def _try_streamed_forward_compat(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk: Any,
    *,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
    agent_override: Any | None = None,
    cancel_requested: Any | None = None,
) -> Optional[Any]:
    """Call _try_streamed_forward with a legacy-signature fallback for tests/plugins."""

    kwargs: dict[str, Any] = {
        "session_mode": session_mode,
        "session_edit_mode": session_edit_mode,
        "cancel_requested": cancel_requested,
    }
    if agent_override is not None:
        kwargs["agent_override"] = agent_override
    try:
        return await _try_streamed_forward(
            app,
            enriched_text,
            sid,
            emit_chunk,
            **kwargs,
        )
    except TypeError as exc:
        if "cancel_requested" not in str(exc):
            raise
        legacy_kwargs: dict[str, Any] = {
            "session_mode": session_mode,
            "session_edit_mode": session_edit_mode,
        }
        if agent_override is not None:
            legacy_kwargs["agent_override"] = agent_override
        return await _try_streamed_forward(
            app,
            enriched_text,
            sid,
            emit_chunk,
            **legacy_kwargs,
        )


def _run_dynamic_agent_compat(
    runner: Any,
    base_agent: Any,
    dynamic_agent: Any,
    question: str,
    sid: str,
    cancel_requested: Any | None,
) -> Any:
    """Run a dynamic agent while preserving older runner call signatures."""

    try:
        return runner(base_agent, dynamic_agent, question, sid, cancel_requested)
    except TypeError as exc:
        if "positional" not in str(exc) and "argument" not in str(exc):
            raise
        return runner(base_agent, dynamic_agent, question, sid)


_OBSERVER_CALL_IDS = threading.local()


class _StreamingOutputError(RuntimeError):
    """Raised when live streaming fails after user-visible output was emitted."""


_STREAM_FALLBACK_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "streaming_dependency_unavailable": {
        "category": "runtime_configuration",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["reconfigure", "retry", "continue_without_live_streaming"],
        "description": "DSPy/LiteLLM streaming dependencies were unavailable.",
    },
    "agent_not_available": {
        "category": "runtime_configuration",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["reconfigure", "retry", "exit"],
        "description": "No executable agent was configured for the session.",
    },
    "agent_not_streamable": {
        "category": "capability_gap",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_without_live_streaming", "reconfigure"],
        "description": "The selected agent is not a DSPy module and cannot emit provider-token deltas.",
    },
    "stream_setup_failed": {
        "category": "streaming_incompatibility",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["retry", "reconfigure", "continue_without_live_streaming"],
        "description": "DSPy stream listener setup failed before user-visible output.",
    },
    "stream_failed_before_output": {
        "category": "provider_streaming_error",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["retry", "reconfigure", "continue_without_live_streaming"],
        "description": "The live provider stream failed before emitting user-visible output.",
    },
    "stream_no_prediction": {
        "category": "streaming_contract_violation",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["retry", "reconfigure", "exit"],
        "description": "DSPy streaming ended without a final prediction.",
    },
    "stream_completed_without_chunks": {
        "category": "provider_streaming_limitation",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_without_live_streaming", "reconfigure", "retry"],
        "description": "DSPy streaming returned a final prediction but no visible token chunks.",
    },
    "provider_streaming_unsupported": {
        "category": "provider_streaming_limitation",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_without_live_streaming", "reconfigure"],
        "description": "The configured provider does not expose a live streaming contract.",
    },
    "sync_execution_path": {
        "category": "non_streamed_execution",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_without_live_streaming", "reconfigure"],
        "description": "The turn completed through the synchronous execution path.",
    },
    "dynamic_prompt_stream_unavailable": {
        "category": "capability_gap",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_without_live_streaming", "reconfigure"],
        "description": "A registered prompt-only agent could not use live streaming.",
    },
    "dynamic_tool_stream_unavailable": {
        "category": "capability_gap",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_without_live_streaming", "reconfigure"],
        "description": "A registered tool agent could not use live streaming.",
    },
}


def _stream_fallback_reason_capabilities() -> dict[str, dict[str, Any]]:
    """Return the audited stream fallback reason catalog for capability metadata."""

    return {
        reason: {
            key: list(value) if isinstance(value, list) else value for key, value in details.items()
        }
        for reason, details in _STREAM_FALLBACK_REASON_DEFINITIONS.items()
    }


_CAPABILITY_GAP_DEFINITIONS: dict[str, dict[str, Any]] = {
    "voice": {
        "status": "unsupported",
        "advertised": False,
        "category": "future_capability",
        "description": (
            "Voice input/output is reserved for future CLIO work and is not "
            "wired to audio capture, transcription, or playback today."
        ),
        "client_behavior": "render_disabled",
        "recovery_actions": ["use_text_input", "hide_or_disable_voice_controls"],
        "related_endpoints": ["/v1/sessions/{sid}/voice/transcribe"],
    },
    "lsp": {
        "status": "unsupported",
        "advertised": False,
        "category": "future_capability",
        "description": (
            "Language-server integration is outside the current CLIO GACT "
            "surface; file and diff workflows are available instead."
        ),
        "client_behavior": "render_disabled",
        "recovery_actions": ["use_files_and_diffs", "hide_or_disable_lsp_controls"],
        "related_endpoints": ["/v1/lsp/*"],
    },
    "optimizer_command": {
        "status": "unavailable",
        "advertised": True,
        "category": "deferred_command",
        "description": (
            "The /optimize slash command is kept visible as future CLIO "
            "direction, but optimizer command execution is not wired yet."
        ),
        "client_behavior": "render_disabled",
        "recovery_actions": [
            "render_optimize_disabled",
            "retry_after_optimizer_support_lands",
        ],
        "related_commands": ["/optimize"],
        "related_endpoints": ["/v1/sessions/{sid}/commands/optimize"],
    },
}


def _capability_gap_metadata() -> dict[str, dict[str, Any]]:
    """Return CLIO capability gaps as client-renderable metadata."""

    return {
        name: {
            key: list(value) if isinstance(value, list) else value for key, value in details.items()
        }
        for name, details in _CAPABILITY_GAP_DEFINITIONS.items()
    }


def _stream_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build structured metadata for a batch text delivery path."""

    definition = _STREAM_FALLBACK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown stream fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: list(value) if isinstance(value, list) else value
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def _stream_fallback_reasons(app: "FastAPI") -> dict[str, dict[str, Any]]:
    reasons = getattr(app.state, "stream_fallback_reasons", None)
    if not isinstance(reasons, dict):
        reasons = {}
        app.state.stream_fallback_reasons = reasons
    return reasons


def _record_stream_fallback(
    app: "FastAPI",
    sid: str,
    reason: str,
    message: str = "",
) -> None:
    _stream_fallback_reasons(app)[sid] = _stream_fallback_payload(reason, message)


def _pop_stream_fallback(app: "FastAPI", sid: str) -> dict[str, Any]:
    return _stream_fallback_reasons(app).pop(sid, {})


def _append_stream_listener(
    listeners: list[Any],
    stream_listener_cls: Any,
    *,
    signature_field_name: str,
    predict: Any,
) -> None:
    if predict is None:
        return
    try:
        listeners.append(
            stream_listener_cls(
                signature_field_name=signature_field_name,
                predict=predict,
            )
        )
    except Exception:  # noqa: BLE001
        return


def _build_stream_listeners(agent: Any, stream_listener_cls: Any) -> list[Any]:
    """Build explicit DSPy stream listeners for CLIO's known predictors.

    Auto-discovering by field name is fragile here because several CLIO
    predictors expose the same output fields. Explicit predictor binding
    lets chat, final synthesis, and expert outputs stream live without
    fighting over repeated names like ``answer`` or ``analysis``.
    """

    listeners: list[Any] = []
    _append_stream_listener(
        listeners,
        stream_listener_cls,
        signature_field_name="answer",
        predict=getattr(agent, "chat_agent", None),
    )
    _append_stream_listener(
        listeners,
        stream_listener_cls,
        signature_field_name="answer",
        predict=getattr(agent, "answer_synthesizer", None),
    )

    for expert_name in ("data_expert", "analysis_expert"):
        expert_predict = getattr(getattr(agent, expert_name, None), "agent", None)
        _append_stream_listener(
            listeners,
            stream_listener_cls,
            signature_field_name="analysis",
            predict=expert_predict,
        )
        _append_stream_listener(
            listeners,
            stream_listener_cls,
            signature_field_name="recommendations",
            predict=expert_predict,
        )

    visualization_extract = getattr(
        getattr(getattr(agent, "visualization_expert", None), "agent", None),
        "extract",
        None,
    )
    visualization_predict = getattr(visualization_extract, "predict", None)
    _append_stream_listener(
        listeners,
        stream_listener_cls,
        signature_field_name="visualization_description",
        predict=visualization_predict,
    )
    _append_stream_listener(
        listeners,
        stream_listener_cls,
        signature_field_name="file_path",
        predict=visualization_predict,
    )
    return listeners


def _agent_streaming_unsupported_reason(agent: Any) -> str:
    """Return a fallback reason when the active provider cannot stream live."""

    provider_config = getattr(agent, "_provider_config", None)
    provider = str(getattr(provider_config, "provider", "") or "")
    if provider in {"argonne", "claude_code", "codex"}:
        return "provider_streaming_unsupported"
    return ""


def _is_placeholder_api_key(value: str | None) -> bool:
    """Return whether an API key is a local no-auth placeholder."""

    return (value or "").strip() in {"", "x", "X", "EMPTY", "empty"}


def _resolve_argonne_runtime_api_key() -> str:
    """Return a fresh ALCF bearer token for runtime provider use."""

    from clio_agent.config import _resolve_argonne_api_key  # noqa: PLC0415

    token = _resolve_argonne_api_key()
    if not token:
        raise RuntimeError("ALCF Globus token is unavailable or could not be refreshed.")
    return token


def _refresh_argonne_lm_token(agent: Any) -> None:
    """Refresh Argonne's short-lived token on live DSPy LM objects."""

    cfg = getattr(agent, "_provider_config", None)
    if cfg is None or getattr(cfg, "provider", "") != "argonne":
        return
    token = _resolve_argonne_runtime_api_key()
    cfg.api_key = token
    for attr in ("_main_lm", "_planner_lm", "_router_lm"):
        lm = getattr(agent, attr, None)
        kwargs = getattr(lm, "kwargs", None)
        if isinstance(kwargs, dict):
            kwargs["api_key"] = token


def _stream_response_prefix(field_name: str, previous_field_name: str) -> str:
    """Return formatting to insert when a streamed output field starts."""

    if not field_name or field_name == previous_field_name:
        return ""
    if field_name == "recommendations":
        return "\n\nRecommendations:\n"
    if field_name == "file_path":
        return "\n\nFile: "
    return ""


async def _try_streamed_forward(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
    agent_override: Any | None = None,
    cancel_requested: Any | None = None,
) -> Optional[Any]:
    """Run the agent's forward via dspy.streamify, pumping every
    text chunk through ``emit_chunk(text)`` as it arrives. Returns
    the final dspy.Prediction on success, or None if streaming is
    unavailable before invoking the agent. Streaming execution failures
    raise ``_StreamingOutputError`` so the caller can surface the failed
    turn instead of rerunning it as batch fallback text.

    Falls back before output when the agent isn't a DSPy module, when
    streamify import fails, or when the wrapped call doesn't yield
    parsable text chunks. The fallback synchronous path produces
    the same wire shape (just no live deltas).
    """

    try:
        import dspy  # noqa: PLC0415
        from dspy.streaming.messages import StreamResponse  # noqa: PLC0415
        from dspy.streaming.streamify import streamify
        from dspy.streaming.streaming_listener import StreamListener  # noqa: PLC0415
        from litellm.types.utils import ModelResponseStream  # noqa: F401
    except Exception as exc:
        _record_stream_fallback(
            app,
            sid,
            "streaming_dependency_unavailable",
            f"{type(exc).__name__}: {exc}",
        )
        return None

    agent = agent_override if agent_override is not None else app.state.agent
    if agent is None:
        _record_stream_fallback(app, sid, "agent_not_available")
        return None
    if not isinstance(agent, dspy.Module):
        _record_stream_fallback(app, sid, "agent_not_streamable")
        return None
    unsupported_reason = _agent_streaming_unsupported_reason(agent)
    if unsupported_reason:
        _record_stream_fallback(app, sid, unsupported_reason)
        return None

    # iowarp/clio-agent#158: bind listeners to explicit Predict instances
    # instead of asking DSPy to infer them by output field name.
    listeners = _build_stream_listeners(agent, StreamListener)
    # is_async_program=True is only valid for modules with a real async
    # forward implementation. dspy.Module exposes acall generically, but
    # its default implementation delegates to aforward; ClioAgent only has
    # sync forward today, so treating inherited acall as sufficient forces
    # streamify into AttributeError and silently drops to synthetic fallback.
    has_async_forward = callable(getattr(agent, "aforward", None))
    try:
        streamed = streamify(
            agent,
            async_streaming=True,
            stream_listeners=listeners,
            is_async_program=has_async_forward,
        )
    except Exception as exc:
        # Stream binding is best-effort. If DSPy cannot attach the
        # listener to this program shape, let the canonical sync path
        # run and surface any real agent/provider error from there.
        _record_stream_fallback(
            app,
            sid,
            "stream_setup_failed",
            f"{type(exc).__name__}: {exc}",
        )
        return None

    final_pred = None
    emitted_any = False
    previous_stream_field = ""

    async def _emit_visible_chunk(text: str, field_name: str = "") -> None:
        nonlocal emitted_any, previous_stream_field
        prefix = _stream_response_prefix(field_name, previous_stream_field)
        if prefix:
            await emit_chunk(prefix)
            emitted_any = True
        await emit_chunk(text)
        emitted_any = True
        if field_name:
            previous_stream_field = field_name

    try:
        # StreamListener emits ``StreamResponse`` instances that
        # carry the cleaned chunk in ``.chunk``. Keep the legacy
        # ``ModelResponseStream`` / dict / str fallback for backends
        # that don't surface a typed listener payload.
        # Pass session_mode + session_edit_mode if the agent's
        # forward signature accepts them (newer ClioAgent does;
        # older / fake agents fall back via TypeError catch).
        try:
            stream_iter = streamed(
                question=enriched_text,
                session_id=sid,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
                cancel_requested=cancel_requested,
            )
        except TypeError:
            try:
                stream_iter = streamed(
                    question=enriched_text,
                    session_id=sid,
                    session_mode=session_mode,
                    session_edit_mode=session_edit_mode,
                )
            except TypeError:
                stream_iter = streamed(question=enriched_text, session_id=sid)
        async for piece in stream_iter:
            if isinstance(piece, dspy.Prediction):
                final_pred = piece
                continue
            if isinstance(piece, StreamResponse):
                if piece.chunk:
                    await _emit_visible_chunk(
                        piece.chunk, getattr(piece, "signature_field_name", "") or ""
                    )
                continue
            text_chunk = _chunk_text(piece)
            if text_chunk:
                await _emit_visible_chunk(text_chunk)
    except Exception as exc:
        if emitted_any:
            raise _StreamingOutputError(
                f"live streaming failed after emitting output: {exc}"
            ) from exc
        _record_stream_fallback(
            app,
            sid,
            "stream_failed_before_output",
            f"{type(exc).__name__}: {exc}",
        )
        raise _StreamingOutputError(f"live streaming failed before emitting output: {exc}") from exc
    if emitted_any and final_pred is None:
        raise _StreamingOutputError(
            "live streaming ended after emitting output without a final prediction"
        )
    if final_pred is None:
        _record_stream_fallback(app, sid, "stream_no_prediction")
    elif not emitted_any:
        _record_stream_fallback(
            app,
            sid,
            "stream_completed_without_chunks",
            "DSPy streamify returned a final prediction but emitted no visible text chunks.",
        )
    return final_pred


def _chunk_text(piece: Any) -> str:
    """Pull a string out of whatever streamify yielded.

    Handles litellm ModelResponseStream + plain str + dict shapes.
    Returns "" when nothing's there (status-message-only chunks
    don't pollute the part body).
    """

    if isinstance(piece, str):
        return piece
    # litellm stream chunks: choices[0].delta.content
    try:
        choices = piece.choices  # type: ignore[attr-defined]
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if content:
                    return str(content)
    except Exception:
        pass
    if isinstance(piece, dict):
        # OpenAI-style dict.
        try:
            return piece["choices"][0]["delta"].get("content", "") or ""
        except (KeyError, IndexError, TypeError):
            return ""
    return ""


def _apply_edit_to_disk(
    *,
    path: str,
    new_content: str,
    session: Any,
    app: "FastAPI",
) -> dict[str, Any]:
    """Write ``new_content`` to ``path`` after enforcing the
    workspace + file_policy boundary.

    The agent's propose_edit tool put the diff together; this is
    the GACT-side commit step the user explicitly approved via
    /v1/sessions/{sid}/diffs/apply. We don't ASK for permission
    (the user already clicked apply) but we DO record an
    auto-approved permission row so /v1/permissions has a
    complete audit trail of every destructive operation.
    """

    target = Path(path).resolve(strict=False)
    # Workspace root scope.
    ws = app.state.workspaces.get(session.workspace_id)
    if ws is not None and ws.root_path:
        try:
            target.relative_to(Path(ws.root_path).resolve())
        except ValueError as exc:
            raise PermissionError(
                f"refused to write {target} outside workspace root {ws.root_path}"
            ) from exc
    # Mode gate — plan + architect can't apply.
    if session.mode in {"plan", "architect"}:
        raise PermissionError(f"refused to write under session.mode={session.mode!r}")
    target = validate_write_path(path, field="path")

    permission_args = {
        "filepath": str(target),
        "new_content_bytes": len(new_content),
    }
    policy_action = _policy_action_for_tool(
        app,
        session_id=session.id,
        session=session,
        tool_name="fs_apply_edit_write",
        args=permission_args,
    )
    if policy_action == "deny":
        _record_resolved_permission(
            app,
            session_id=session.id,
            tool_name="fs_apply_edit_write",
            args=permission_args,
            status="auto_denied",
            action="deny",
            summary=f"diffs/apply blocked by permission policy for {target}",
            reason="policy_deny",
        )
        raise PermissionError(
            f"refused to write {target} because a permission policy denied fs_apply_edit_write"
        )

    # Audit row for the apply (auto-approved by the user's explicit
    # POST to /diffs/apply). Every destructive call lands in
    # /v1/permissions for compliance / replay.
    _record_resolved_permission(
        app,
        session_id=session.id,
        tool_name="fs_apply_edit_write",
        args=permission_args,
        status="auto_approved",
        action="allow",
        summary=f"diffs/apply: write {len(new_content)} bytes to {target}",
        reason="user_clicked_apply",
    )

    return write_text_with_policy(str(target), new_content)


def _enrich_with_context_files(app: "FastAPI", sid: str, user_text: str) -> str:
    """Prepend a "Context:" section to the user's text for every
    file attached to the session via /v1/sessions/{sid}/context/files.

    Behaviour by mode:
      - read / pin: read up to ``_CTX_MAX_BYTES`` from disk + inline.
      - edit: include path + size hint only (the agent fetches via
        a tool when it needs the body).

    Read/pin files are requested context. If they cannot be resolved,
    found, inspected, or read, the turn raises a structured error
    instead of proceeding with missing context. Edit entries can
    point at files that do not exist yet, so they stay visible as
    edit targets without requiring a body.

    Returns the original ``user_text`` unchanged when no files are
    attached.
    """

    files = (app.state.context_files.get(sid, {}) or {}).values()
    if not files:
        return user_text

    blocks: list[str] = []
    for row in files:
        path_str = row.get("resolved_path") or row.get("path") or ""
        display_path = row.get("display_path") or row.get("path") or path_str
        if not path_str:
            continue
        for marker in {f"@{display_path}", f"@{row.get('path') or ''}", f"@{Path(display_path).name}"}:
            if marker != "@":
                user_text = user_text.replace(marker, display_path)
        mode = row.get("mode") or "read"
        try:
            p = Path(path_str).resolve()
        except (OSError, ValueError) as exc:
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="resolve",
                message=f"Could not resolve attached context file: {path_str}",
                original_error=exc,
            ) from exc
        # iowarp/clio-agent#5: do NOT silently skip files outside the
        # workspace root — the user explicitly attached this file via
        # POST /v1/sessions/{sid}/context/files, so they know what
        # they're doing. The destructive-write gates (workspace root
        # in _apply_edit_to_disk, plus mode=plan/architect) still
        # protect against unintended writes.
        if mode == "edit" and not p.exists():
            blocks.append(f"### Context file: {display_path} (mode=edit, target does not exist yet)")
            continue
        if not p.exists():
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="exists",
                message=f"Attached context file no longer exists: {path_str}",
            )
        if not p.is_file():
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="is_file",
                message=f"Attached context path is not a file: {path_str}",
            )
        try:
            size = p.stat().st_size
        except OSError as exc:
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="stat",
                message=f"Could not stat attached context file: {path_str}",
                original_error=exc,
            ) from exc
        header = f"### Context file: {display_path} (mode={mode}, {size} bytes)"
        if mode == "edit":
            blocks.append(header)
            continue
        # Scientific binary files (parquet/hdf5) don't decode as
        # useful text — dumping raw bytes leaves the LM blind. Run
        # the bundled inspection tool and inline the structured
        # summary instead. Generic mechanism: an extension → fn map.
        suffix = p.suffix.lower()
        binary_inspector = _BINARY_CONTEXT_INSPECTORS.get(suffix)
        if binary_inspector is not None:
            try:
                summary = binary_inspector(str(p))
                blocks.append(header + "\n```\n" + summary + "\n```")
                continue
            except Exception as exc:  # noqa: BLE001
                raise _context_file_access_error(
                    path=path_str,
                    mode=mode,
                    operation="inspect",
                    message=(f"Could not inspect attached binary context file: {path_str}"),
                    original_error=exc,
                ) from exc
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="read",
                message=f"Could not read attached context file: {path_str}",
                original_error=exc,
            ) from exc
        if len(data) > _CTX_MAX_BYTES:
            blocks.append(
                header
                + "\n```\n"
                + data[:_CTX_MAX_BYTES].decode("utf-8", errors="replace")
                + f"\n... ({len(data) - _CTX_MAX_BYTES} more bytes truncated)\n```"
            )
        else:
            blocks.append(header + "\n```\n" + data.decode("utf-8", errors="replace") + "\n```")

    if not blocks:
        return user_text
    return (
        "## Attached files (auto-prepended from session context)\n\n"
        + "\n\n".join(blocks)
        + "\n\n## User question\n\n"
        + user_text
    )


def _memory_search_request_from_message(message: "Message", user_text: str) -> dict[str, Any] | None:
    raw = message.metadata.get("memory_search") if isinstance(message.metadata, Mapping) else None
    if raw is None and isinstance(message.metadata, Mapping):
        if not message.metadata.get("include_cross_session_memory"):
            return None
        raw = {
            "enabled": True,
            "query": message.metadata.get("memory_search_query") or user_text,
            "include_cross_session": True,
            "reason": message.metadata.get("memory_search_reason") or "",
        }
    if not isinstance(raw, Mapping):
        return None
    if raw.get("enabled") is False:
        return None
    return dict(raw)


def _enrich_with_requested_memory_search(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
) -> tuple[str, dict[str, Any]]:
    """Prepend explicitly requested memory-search hits to one turn.

    This is intentionally opt-in through user message metadata. It gives the
    orchestrator/TUI a tool-like way to make cross-session recall visible to the
    model without weakening the default per-session context boundary.
    """

    req = _memory_search_request_from_message(user_msg, user_text)
    if req is None:
        return user_text, {}

    query = str(req.get("query") or user_text).strip()
    include_cross_session = bool(req.get("include_cross_session", False))
    workspace_id = str(req.get("workspace_id") or "").strip()
    reason = str(req.get("reason") or "").strip()
    try:
        limit = int(req.get("limit", 5) or 5)
    except (TypeError, ValueError):
        limit = 5
    response = _memory_search_response(
        app,
        query=query,
        session_id=sid,
        workspace_id=workspace_id,
        include_cross_session=include_cross_session,
        limit=limit,
        exclude_message_id=user_msg.id,
    )
    metadata = {
        "query": response.query,
        "include_cross_session": response.include_cross_session,
        "searched_sessions": response.searched_sessions,
        "hit_count": len(response.hits),
        "reason": reason,
        "scope": response.metadata.get("scope", ""),
        "hits": [
            {
                "session_id": hit.session_id,
                "session_title": hit.session_title,
                "message_id": hit.message_id,
                "part_id": hit.part_id,
                "role": hit.role,
                "match_terms": hit.match_terms,
                "score": hit.score,
                "cross_session": bool(hit.metadata.get("cross_session", False)),
            }
            for hit in response.hits
        ],
    }
    app.state.bus.publish(
        Event(
            type="memory.search.completed",
            session_id=sid,
            payload=metadata,
        )
    )
    if not response.hits:
        return user_text, metadata

    blocks = []
    for idx, hit in enumerate(response.hits, start=1):
        cross = "cross-session" if hit.metadata.get("cross_session") else "current-session"
        title = hit.session_title or hit.session_id
        blocks.append(
            f"### Memory hit {idx}: {title} ({cross})\n"
            f"- session_id: {hit.session_id}\n"
            f"- message_id: {hit.message_id}\n"
            f"- role: {hit.role}\n"
            f"- matched_terms: {', '.join(hit.match_terms)}\n"
            f"```\n{hit.text}\n```"
        )
    return (
        "## Explicit Memory Search Results\n\n"
        + f"Query: {response.query}\n"
        + f"Reason: {reason or 'not provided'}\n"
        + f"Scope: {metadata['scope']}\n\n"
        + "\n\n".join(blocks)
        + "\n\n## User question\n\n"
        + user_text
    ), metadata


def _context_file_turn_provenance(app: "FastAPI", sid: str, *, status: str) -> dict[str, Any]:
    """Return non-secret provenance for context files attached to this turn."""

    rows = list((app.state.context_files.get(sid, {}) or {}).values())
    files: list[dict[str, Any]] = []
    for row in rows:
        path = str(row.get("path") or "")
        if not path:
            continue
        mode = str(row.get("mode") or "read")
        file_row: dict[str, Any] = {
            "path": path,
            "mode": mode,
            "status": status,
            "inline_policy": "metadata_only" if mode == "edit" else "inline_or_inspect",
        }
        for key in ("source", "workspace_id", "display_path", "resolved_path", "added_at"):
            value = row.get(key)
            if value:
                file_row[key] = value
        if row.get("size") is not None:
            file_row["size"] = row.get("size")
        files.append(file_row)
    return {
        "status": status,
        "count": len(files),
        "max_inline_bytes": _CTX_MAX_BYTES,
        "files": files,
    }


_CTX_MAX_BYTES = 32 * 1024  # 32 KB cap per attached file


def _inspect_parquet_for_context(path: str) -> str:
    """Run analyze_schema on a Parquet file + return a one-paragraph
    summary the LM can quote when answering 'what's in this file'."""

    from clio_agent.tools.servers.parquet_server import analyze_schema

    fn = getattr(analyze_schema, "fn", analyze_schema)
    schema = fn(path)
    if "error" in schema:
        return f"Could not inspect Parquet file: {schema['error']}"
    cols = schema.get("columns", []) or []
    col_lines = [
        f"  - {c.get('name')}: {c.get('type')}, nullable={c.get('nullable')}" for c in cols[:24]
    ]
    body = (
        f"Parquet file with {schema.get('num_rows', '?')} rows, "
        f"{schema.get('num_columns', '?')} columns, "
        f"{schema.get('num_row_groups', '?')} row groups.\n"
        "Schema:\n" + "\n".join(col_lines)
    )
    if len(cols) > 24:
        body += f"\n  - ... {len(cols) - 24} more columns"
    return body


def _inspect_hdf5_for_context(path: str) -> str:
    """Run analyze_file + list_datasets on an HDF5 file + return a
    one-paragraph summary."""

    from clio_agent.tools.servers.hdf5_server import (
        analyze_file,
        list_datasets,
    )

    af = getattr(analyze_file, "fn", analyze_file)
    ld = getattr(list_datasets, "fn", list_datasets)
    overview = af(path)
    datasets = ld(path)
    if "error" in overview:
        return f"Could not inspect HDF5 file: {overview['error']}"
    rows = (datasets.get("datasets", []) if isinstance(datasets, dict) else []) or []
    ds_lines = [
        f"  - {d.get('path')}: shape={d.get('shape')} dtype={d.get('dtype')}" for d in rows[:24]
    ]
    body = (
        f"HDF5 file with {overview.get('total_datasets', len(rows))} datasets "
        f"in {overview.get('total_groups', 0)} groups.\n"
        "Datasets:\n" + "\n".join(ds_lines)
    )
    if len(rows) > 24:
        body += f"\n  - ... {len(rows) - 24} more datasets"
    return body


_BINARY_CONTEXT_INSPECTORS = {
    ".parquet": _inspect_parquet_for_context,
    ".pq": _inspect_parquet_for_context,
    ".h5": _inspect_hdf5_for_context,
    ".hdf5": _inspect_hdf5_for_context,
}


def _format_react_trajectory(traj: Any) -> str:
    """Render a DSPy ReAct trajectory (a list/dict of steps) as a
    human-readable trace. Returns "" when the input doesn't look
    like a trajectory.
    """

    if not traj:
        return ""
    rows: list[str] = []
    if isinstance(traj, dict):
        # ReAct stores as {step_n_thought, step_n_action, ...}
        idx = 0
        while True:
            thought = traj.get(f"step_{idx}_thought") or traj.get(f"thought_{idx}")
            action = traj.get(f"step_{idx}_tool_name") or traj.get(f"action_{idx}")
            if thought is None and action is None:
                break
            row = []
            if thought:
                row.append(f"thought: {thought}")
            if action:
                row.append(f"action: {action}")
            rows.append("  ".join(row))
            idx += 1
    elif isinstance(traj, list):
        for i, step in enumerate(traj):
            if isinstance(step, dict):
                rows.append(f"step {i}: {step}")
            else:
                rows.append(f"step {i}: {step!r}")
    return "\n".join(rows)


def _extract_tools_called(pred: Any) -> list[dict[str, Any]]:
    """Pull an agent prediction's tool-call trace into a wire-shaped
    list.

    The tier-2 experts expose their tool calls on
    ``pred.tools_called`` when the ReAct loop tracks them. Each
    entry is either a ``clio_agent.arc.schema.ToolCall`` (msgspec
    struct), a plain dict, or an object with attribute access —
    handle all three. Fields copied onto the wire when present:
    name, args, ok, duration_ms, cached. All optional.
    """

    raw = getattr(pred, "tools_called", None)
    if not raw:
        return []

    out: list[dict[str, Any]] = []
    for call in raw:
        row: dict[str, Any] = {}
        agent_trace_call = False
        if isinstance(call, dict):

            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return _src.get(key, default)
        else:
            # msgspec structs + DSPy trace records — attribute access.
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return getattr(_src, key, default)

            agent_trace_call = (
                hasattr(call, "tool") and hasattr(call, "params") and hasattr(call, "result")
            )

        name = get("name") or get("tool") or ""
        if name:
            row["name"] = str(name)

        args = get("args")
        if args is None:
            args = get("arguments")
        if args is None:
            args = get("params")
        if args is not None:
            row["args"] = args

        status = get("status")
        if status is not None:
            row["ok"] = status not in {"failure", "error", "timeout"}
        elif get("ok") is not None:
            row["ok"] = bool(get("ok"))

        duration_ms = get("duration_ms")
        if duration_ms is not None:
            row["duration_ms"] = float(duration_ms)

        cached = get("cached")
        if cached is not None:
            row["cached"] = bool(cached)

        result = get("result")
        if result is not None:
            row["result"] = result
            if "ok" not in row and agent_trace_call:
                row["ok"] = not (
                    (isinstance(result, dict) and "error" in result)
                    or (isinstance(result, str) and result.startswith("Error:"))
                )

        telemetry_source = get("telemetry_source") or (
            "agent_trace" if agent_trace_call else "posthoc_prediction"
        )
        row["telemetry_source"] = str(telemetry_source)

        if row:
            out.append(row)
    return out


# CLIO-BBBBBBBBBB10: mapping from CLIO expert id to its GACT v0.2
# specialization tag. Free-form (UI palette hint); picked to match
# the emulator's generic "code_editing / data_analysis /
# knowledge_retrieval / visualization" vocab the TUI already
# colour-codes.
_EXPERT_SPECIALIZATION: dict[str, str] = {
    "data": "data_analysis",
    "ndp_catalog": "knowledge_retrieval",
    "analysis": "data_analysis",
    "sac_format": "data_analysis",
    "visualization": "data_visualization",
    "utility": "utility",
}

# CLIO-BBBBBBBBBB10: per-expert curated tool list. CLIO's Expert
# classes attach their tools at construction time (via
# MCPToolBridge.to_dspy_tools()), but we don't want to import DSPy +
# spin up tool servers just to list a catalog. The tool sets are
# stable so hardcoding the mapping here is cheap + honest; if an
# expert's tool set drifts, the test_agents_catalog test fails and
# we update both sides at once.
_EXPERT_TOOLS: dict[str, list[str]] = {
    "data": [
        "hdf5_list_datasets",
        "hdf5_analyze_dataset",
        "hdf5_check_compression",
        "hdf5_optimize_chunking",
        "hdf5_analyze_file",
        "adios_inspect_file",
        "adios_inspect_variables",
        "adios_inspect_profiling",
    ],
    "ndp_catalog": [
        "ndp_list_organizations",
        "ndp_search_datasets",
        "ndp_get_dataset_details",
        "ndp_stage_resource",
    ],
    "sac_format": [
        "sac_inspect_archive",
        "sac_compute_trace_statistics",
        "sac_plot_traces",
    ],
    "analysis": [
        "parquet_analyze_schema",
        "parquet_query_data",
        "parquet_compute_statistics",
        "csv_read_table",
    ],
    "visualization": [
        "plot_histogram",
        "plot_bar_chart",
        "plot_scatter",
        "plot_summary",
    ],
    "utility": [
        "shell_bash",
        "fs_propose_edit",
    ],
}

_EXPERT_CAPABILITIES: dict[str, dict[str, Any]] = {
    "data": {
        "name": "Data Expert",
        "description": (
            "Specializes in scientific data files and discovery: HDF5, ADIOS/BP, "
            "compression strategies, I/O performance, format conversion, and "
            "delegation to nested catalog and format experts."
        ),
        "keywords": [
            "hdf5",
            "adios",
            "bp5",
            "dataset discovery",
            "catalog",
            "compression",
            "chunking",
            "data format",
            "file optimization",
            "i/o performance",
            "parallel io",
            "mpi-io",
        ],
        "metadata": {
            "delegates_to": [
                "HDF5 tools",
                "ADIOS/BP tools",
                "NDP catalog expert",
                "SAC format expert",
            ],
            "routes_to": ["ndp_catalog"],
        },
    },
    "ndp_catalog": {
        "name": "NDP Catalog Expert",
        "description": (
            "Nested data expert for National Data Platform and EarthScope-style "
            "dataset discovery, metadata inspection, resource ranking, and bounded staging."
        ),
        "keywords": [
            "ndp",
            "national data platform",
            "earthscope",
            "dataset discovery",
            "catalog",
            "resource",
            "staging",
        ],
        "metadata": {
            "parent": "data",
            "route_type": "tier_3_catalog_expert",
            "future_model_boundary": True,
        },
        "tier": 3,
    },
    "analysis": {
        "name": "Analysis Expert",
        "description": (
            "Specializes in statistical analysis, data profiling, and quality "
            "assessment of tabular datasets (Parquet/CSV). Coordinates nested format "
            "experts for waveform and domain-specific files."
        ),
        "keywords": [
            "parquet",
            "csv",
            "statistics",
            "analysis",
            "schema",
            "distribution",
            "data quality",
            "columnar",
            "profiling",
            "null count",
            "outliers",
        ],
        "metadata": {
            "delegates_to": [
                "parallel nanoagents for independent file checks",
                "SAC format expert",
            ],
        },
    },
    "sac_format": {
        "name": "SAC Format Expert",
        "description": (
            "Nested format expert for SAC waveform archives. Inspects SAC members, "
            "computes trace statistics, and provides plot-ready waveform outputs."
        ),
        "keywords": ["sac", "waveform", "trace", "seismology", "seismic"],
        "metadata": {
            "parent": "analysis",
            "route_type": "tier_3_format_expert",
            "future_model_boundary": True,
        },
        "tier": 3,
    },
    "visualization": {
        "name": "Visualization Expert",
        "description": (
            "Specializes in generating scientific data visualizations: "
            "histograms, scatter plots, bar charts, and summary dashboards "
            "from tabular datasets (Parquet, CSV), plus delegated waveform trace plots. "
            "Saves charts to disk as PNG."
        ),
        "keywords": [
            "visualization",
            "plot",
            "chart",
            "histogram",
            "scatter",
            "distribution",
            "bar chart",
            "graph",
        ],
        "metadata": {
            "delegates_to": ["matplotlib plotting tools"],
        },
    },
    "utility": {
        "name": "Utility Expert",
        "description": (
            "Exposes local permission-gated utility tools for simple shell "
            "diagnostics such as current time or environment checks."
        ),
        "keywords": [
            "shell",
            "bash",
            "terminal",
            "command",
            "time",
            "date",
            "environment",
        ],
        "metadata": {
            "delegates_to": ["permission-gated shell tool", "workspace edit proposal tool"],
        },
    },
}


_BUILTIN_SYSTEM_PROMPTS: dict[str, str] = {
    "main": (
        "You are CLIO's agent planner. You control a tool-using scientific data "
        "agent and route user requests to the correct specialist or tool path."
    ),
    "data": (
        "You are the CLIO Data Expert, a specialized autonomous agent for "
        "scientific data file formats, storage optimization, I/O performance, "
        "and external dataset discovery."
    ),
    "ndp_catalog": (
        "You are the CLIO NDP Catalog Expert, a nested data agent for National "
        "Data Platform and EarthScope-style dataset discovery and bounded staging."
    ),
    "analysis": (
        "You are the CLIO Analysis Expert, a specialized autonomous agent for "
        "statistical analysis, data profiling, and data quality."
    ),
    "sac_format": (
        "You are the CLIO SAC Format Expert, a nested format agent for SAC "
        "waveform archive inspection, trace statistics, and plot-ready outputs."
    ),
    "visualization": (
        "You are the CLIO Visualization Expert, a specialized autonomous agent for "
        "generating scientific data visualizations from tool-grounded data."
    ),
    "utility": (
        "You are the CLIO Utility Expert. Use permission-gated utility tools for "
        "simple shell diagnostics and environment checks."
    ),
}


def _signature_prompt(signature: Any) -> str:
    """Return a cleaned DSPy signature docstring for catalog display."""
    return inspect.cleandoc(getattr(signature, "__doc__", "") or "")


def _builtin_agents() -> list[AgentDef]:
    """Return CLIO's built-in tier-2 experts as AgentDef rows.

    Imports are lazy inside the function because importing
    clio_agent.experts at module load time pulls in DSPy + the
    tool bridges — heavy, and we don't want it to explode scaffold
    tests if DSPy isn't available. Each expert exposes
    ``get_capabilities()`` returning ``{name, description, keywords,
    tools}``; we map those onto the GACT AgentDef shape.

    A tier-1 orchestrator row ('main') is synthesised so the TUI
    can see the full hierarchy; its tools list is empty (the
    orchestrator dispatches rather than acting itself).
    """

    rows: list[AgentDef] = [
        AgentDef(
            id="main",
            source="builtin",
            title="Main Agent",
            description=(
                "Tier-1 orchestrator. Routes user queries to tier-2 "
                "specialists based on keyword heuristics + LM classifier."
            ),
            system_prompt=_BUILTIN_SYSTEM_PROMPTS["main"],
            parent_id="",
            tier=1,
            specialization="orchestrator",
            metadata={
                "routes_to": sorted(_EXPERT_CAPABILITIES),
                "route_type": "tier_1_orchestrator",
            },
        ),
    ]

    for expert_id, caps in _EXPERT_CAPABILITIES.items():
        name = caps.get("name", expert_id.replace("_", " ").title())
        description = caps.get("description", "")
        keywords = list(caps.get("keywords", []))
        tools = list(_EXPERT_TOOLS.get(expert_id, []))
        rows.append(
            AgentDef(
                id=expert_id,
                source="builtin",
                title=name,
                description=description,
                parent_id=str(
                    caps.get("parent_id") or caps.get("metadata", {}).get("parent") or "main"
                ),
                system_prompt=_BUILTIN_SYSTEM_PROMPTS.get(expert_id, ""),
                tools=tools,
                tier=int(caps.get("tier", 2)),
                specialization=_EXPERT_SPECIALIZATION.get(expert_id, expert_id),
                keywords=keywords,
                metadata=dict(caps.get("metadata", {})),
            )
        )

    return rows


def _load_skills_from_disk() -> list[AgentDef]:
    """Discover local skill files and register each as ``source="skill"``.

    Supported layouts are intentionally bounded to known skill roots:
    - Claude flat/project skills: ``.claude/skills/*.md``
    - Directory skills: ``.claude/skills/**/SKILL.md``
    - Codex skills: ``.codex/skills/**/SKILL.md``
    - Agent skills: ``.agents/skills/**/SKILL.md``

    User-global roots are scanned first and project-local roots second so a
    project skill with the same id overrides a global skill. The body after
    frontmatter is used as the skill's system prompt.
    """
    import os
    from pathlib import Path

    rows: dict[str, AgentDef] = {}
    for root, source in _skill_search_roots(Path.home(), Path(os.getcwd())):
        if not root.exists() or not root.is_dir():
            continue
        for md in _skill_markdown_files(root):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            meta, body = _parse_skill_frontmatter(text)
            sid = (meta.get("name") or _default_skill_id(md)).strip()
            if not sid:
                continue
            description = str(meta.get("description") or "").strip()
            if not description and body:
                # Fall back to the first non-blank line of the body.
                for line in body.splitlines():
                    line = line.strip()
                    if line:
                        description = line[:240]
                        break

            tools = _skill_list_field(meta, "allowed-tools", "allowed_tools")
            keywords = _skill_list_field(meta, "keywords", "tags")
            if not keywords:
                keywords = _fallback_skill_keywords(sid)

            metadata = {
                "skill_path": str(md),
                "skill_dir": str(md.parent if md.name.upper() == "SKILL.MD" else root),
                "skill_layout": "skill_md" if md.name.upper() == "SKILL.MD" else "flat_md",
                "skill_source": source,
            }
            if meta.get("model"):
                metadata["model"] = str(meta["model"]).strip()
            for key in (
                "command",
                "slash_command",
                "slash-command",
                "commands",
                "slash_commands",
                "slash-commands",
                "prompt_template",
                "prompt-template",
            ):
                if key in meta:
                    metadata[key] = meta[key]
            if body:
                # Stash the system-prompt body so future /v1/agents/{id}
                # can return the full prompt without re-reading the file.
                metadata["system_prompt"] = body

            rows[sid] = AgentDef(
                id=sid,
                source="skill",
                title=str(meta.get("title") or sid).strip(),
                description=description,
                system_prompt=body,
                default_provider=str(meta.get("provider", "") or "").strip(),
                default_model=str(meta.get("model", "") or "").strip(),
                tools=tools,
                tier=2,
                specialization="skill",
                keywords=keywords,
                metadata=metadata,
            )
    return list(rows.values())


def _load_command_files_from_disk() -> list[dict[str, Any]]:
    """Discover CLIO/Claude-compatible Markdown command recipe files."""
    import os
    from pathlib import Path

    rows: dict[str, dict[str, Any]] = {}
    for root, source in _command_search_roots(Path.home(), Path(os.getcwd())):
        if not root.exists() or not root.is_dir():
            continue
        for md in sorted(root.glob("*.md"), key=lambda path: str(path).lower()):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            meta, body = _parse_skill_frontmatter(text)
            command_id = _normalize_file_command_id(meta, md)
            if not command_id:
                continue
            description = str(meta.get("description") or "").strip()
            if not description:
                for line in body.splitlines():
                    line = line.strip()
                    if line:
                        description = line[:240]
                        break

            status = str(meta.get("status") or "available").strip() or "available"
            disabled_reason = str(
                meta.get("disabled_reason") or meta.get("disabled-reason") or ""
            ).strip()
            shell_fields = ("shell", "exec", "run", "command_line", "command-line")
            if any(key in meta for key in shell_fields):
                status = "unsupported"
                disabled_reason = disabled_reason or (
                    "direct local shell execution is not supported by CLIO user commands"
                )

            enabled = _truthy_command_field(meta.get("enabled"), status == "available")
            if status != "available":
                enabled = False
            agent_id = str(
                meta.get("agent")
                or meta.get("agent_id")
                or meta.get("target_agent")
                or meta.get("target-agent")
                or "main"
            ).strip()
            command = {
                "id": command_id,
                "title": str(meta.get("title") or command_id).strip(),
                "description": description,
                "source": "user",
                "status": status,
                "enabled": enabled,
                "error": str(
                    meta.get("error")
                    or ("not_supported" if status == "unsupported" else "")
                ),
                "disabled_reason": disabled_reason,
                "agent_id": agent_id,
                "agent_source": "command_file",
                "command_path": str(md),
                "command_source": source,
                "invocation": (
                    "agent"
                    if _truthy_command_field(meta.get("agent-invocable"), True)
                    else "user"
                ),
                "user_invocable": _truthy_command_field(meta.get("user-invocable"), True),
                "agent_invocable": _truthy_command_field(meta.get("agent-invocable"), True),
                "argument_hint": str(
                    meta.get("argument-hint") or meta.get("argument_hint") or ""
                ),
                "arguments": meta.get("arguments") or [],
                "prompt_template": body,
                "prompt_profile": str(
                    meta.get("prompt-profile") or meta.get("prompt_profile") or ""
                ),
            }
            rows.setdefault(command_id, command)
    return list(rows.values())


def _skill_search_roots(home: Path, cwd: Path) -> list[tuple[Path, str]]:
    """Return skill roots in override order."""
    return [
        (home / ".claude" / "skills", "claude"),
        (home / ".codex" / "skills", "codex"),
        (home / ".agents" / "skills", "agents"),
        (cwd / ".claude" / "skills", "claude"),
        (cwd / ".codex" / "skills", "codex"),
        (cwd / ".agents" / "skills", "agents"),
    ]


def _command_search_roots(home: Path, cwd: Path) -> list[tuple[Path, str]]:
    """Return command roots in precedence order; first matching id wins."""
    return [
        (cwd / ".clio" / "commands", "clio_workspace"),
        (cwd / ".claude" / "commands", "claude_workspace"),
        (home / ".config" / "clio-agent" / "commands", "clio_user"),
        (home / ".claude" / "commands", "claude_user"),
    ]


def _normalize_file_command_id(meta: Mapping[str, Any], path: Path) -> str:
    raw = meta.get("slash_id") or meta.get("slash-id") or meta.get("name") or path.stem
    value = str(raw or "").strip()
    if not value:
        return ""
    return value if value.startswith("/") else f"/{value}"


def _truthy_command_field(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def _skill_markdown_files(root: Path) -> list[Path]:
    """Return candidate skill markdown files under a known skill root."""
    candidates: dict[str, Path] = {}
    for pattern in ("*.md", "**/SKILL.md"):
        for path in root.glob(pattern):
            if path.is_file():
                candidates[str(path.resolve(strict=False)).lower()] = path
    return sorted(candidates.values(), key=lambda path: str(path).lower())


def _default_skill_id(path: Path) -> str:
    """Return a stable skill id when frontmatter does not specify one."""
    if path.name.upper() == "SKILL.MD":
        return path.parent.name
    return path.stem


def _skill_list_field(meta: dict[str, Any], *keys: str) -> list[str]:
    """Coerce comma-separated or frontmatter-list fields into strings."""
    value: Any = None
    for key in keys:
        if key in meta:
            value = meta[key]
            break
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _fallback_skill_keywords(skill_id: str) -> list[str]:
    """Return search keywords for minimal skill files without frontmatter tags."""
    return [
        part for part in skill_id.replace("-", " ").replace("_", " ").split() if part.strip()
    ] or [skill_id]


def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body) for a SKILL.md.

    Recognises the standard ``---``-delimited block at the head of the
    file. Falls back to ({}, text) when no frontmatter is present.
    Uses a tiny line-by-line parser instead of pulling PyYAML in as a
    dependency: frontmatter shapes we care about are flat key:value plus
    optional ``- item`` lists, well within hand-rolling distance.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    cur_key: Optional[str] = None
    for raw in lines[1:end]:
        if raw.startswith("- "):
            if cur_key and isinstance(meta.get(cur_key), list):
                meta[cur_key].append(raw[2:].strip())
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if not value:
            meta[key] = []
            cur_key = key
        else:
            meta[key] = value.strip("\"'")
            cur_key = None
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def _builtin_tools() -> list[Tool]:
    """Flatten the experts' curated tool lists into a single GACT
    Tool catalog. Stable ids (same strings the experts reference),
    backend flag `builtin`. The names MAY duplicate across experts
    (e.g. read_file) — we dedupe by id so GET /v1/catalog/tools has
    one row per distinct tool."""

    seen: dict[str, Tool] = {}
    for agent in _builtin_agents():
        if agent.tier not in {2, 3}:
            continue
        for tool_name in agent.tools:
            if tool_name in seen:
                continue
            seen[tool_name] = Tool(
                id=tool_name,
                source="builtin",
                name=tool_name,
                title=tool_name.replace("_", " ").title(),
                owner=_tool_owner_for_catalog(tool_name),
                tags=_tool_tags_for_catalog(tool_name),
                visible_to=_tool_visible_to_for_catalog(tool_name),
            )
    return list(seen.values())


def _tool_owner_for_catalog(tool_name: str) -> str:
    """Return static owner metadata for a catalog tool row."""
    try:
        from clio_agent.tools.catalog import tool_owner

        return tool_owner(tool_name)
    except Exception:
        return ""


def _tool_tags_for_catalog(tool_name: str) -> list[str]:
    """Return static tag metadata for a catalog tool row."""
    try:
        from clio_agent.tools.catalog import tool_tags

        return sorted(tool_tags(tool_name))
    except Exception:
        return []


def _tool_visible_to_for_catalog(tool_name: str) -> list[str]:
    """Return static visibility metadata for a catalog tool row."""
    try:
        from clio_agent.tools.catalog import tool_visible_scopes

        return tool_visible_scopes(tool_name)
    except Exception:
        return []


from typing import Protocol

from clio_agent.gact.events import Event, EventBus, heartbeat_payload
from clio_agent.gact.expert_packs import (
    discover_expert_packs,
    load_expert_pack_path,
    load_expert_packs,
    validate_expert_hierarchy,
    validate_expert_pack_path,
)
from clio_agent.gact.messages import MessageStore
from clio_agent.gact.sessions import SessionStore, _default_store_path
from clio_agent.gact.types import (
    AgentCapabilityRef,
    AgentDef,
    AnswerUserQuestionRequest,
    AuthInfo,
    BackendInfo,
    CacheStats,
    Capabilities,
    CapabilityFlags,
    CreateSessionRequest,
    CreateUserQuestionRequest,
    CreateWorkspaceRequest,
    ErrorEnvelope,
    ErrorInfo,
    GlobalMemoryStats,
    HealthResponse,
    Integration,
    ListAgentsResponse,
    ListSessionsResponse,
    ListToolsResponse,
    ListWorkspacesResponse,
    LMProviderInfo,
    LMProviderPreset,
    LMProviderRequest,
    MemorySearchHit,
    MemorySearchResponse,
    MemoryStats,
    Message,
    Metrics,
    MetricsMessages,
    MetricsSessions,
    ModelRef,
    Part,
    PostMessageRequest,
    PostMessageResponse,
    RetryTurnRequest,
    Session,
    SessionContextPolicy,
    SessionMemoryStats,
    Tokens,
    Tool,
    TransportFlags,
    TurnAttempt,
    UpdateSessionRequest,
    UserQuestion,
    UserQuestionOption,
    Workspace,
)
from clio_agent.gact.workspaces import (
    WorkspaceStore,
)
from clio_agent.gact.workspaces import (
    _default_store_path as _ws_default_store_path,
)


class AgentLike(Protocol):
    """Structural interface for anything the GACT POST-message path
    can drive. Lets tests inject a fake without pulling DSPy + a real
    LM; production wires the actual ``ClioAgent``.

    ``forward`` MUST return something with ``.answer`` (str) and
    ``.selected_expert`` (str). The real ``dspy.Prediction`` already
    matches this shape; FakeClioAgent in the tests does too.
    """

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover
        ...


# Version pins. Keep in sync with the gact-tui SPEC.md version bump
# history; bump EMULATOR_VERSION-equivalent here only when the
# *module's* behaviour changes, not every spec revision.
CONTRACT_VERSION = "0.2"
GACT_BACKEND_VERSION = "0.1.0"  # version of this clio_agent.gact module


def _not_implemented(capability: str) -> ErrorEnvelope:
    """Build the v0.2 error envelope for a 501 response."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="config_error",
            message=f"capability not yet implemented: {capability}",
            details={
                "capability": capability,
                "note": (
                    "This endpoint is stubbed at CLIO-BBBBBBBBBB6; it will "
                    "be wired in a follow-on iteration. See "
                    "gact-tui/PLAN.md phase CLIO-BBBBBBBBBB for the roadmap."
                ),
            },
            recoverable=False,
        )
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook.

    Spins the scheduler tick task (#21) at boot if a ScheduleStore
    is wired; cancels it cleanly on shutdown.

    Also kicks off deferred ClioAgent construction when the runner
    set ``app.state.want_agent`` (see ``main()``). The agent's heavy
    init (DSPy + ARC + experts) used to block uvicorn's startup, which
    pushed first /v1/capabilities response past gact-tui's 3-second
    deploy probe. Now we bind the port immediately, finish boot in a
    background task, and POST /messages keeps 503-ing until
    ``app.state.agent`` is stamped.
    """

    app.state.started_at = time.time()
    task: Optional[asyncio.Task] = None
    if getattr(app.state, "schedules", None) is not None:
        task = asyncio.create_task(_scheduler_tick(app))
        app.state.scheduler_task = task

    agent_task: Optional[asyncio.Task] = None
    if getattr(app.state, "want_agent", False) and app.state.agent is None:
        agent_task = asyncio.create_task(_construct_agent_async(app))
        app.state.agent_construction_task = agent_task

    yield

    lm_config_task = getattr(app.state, "lm_config_task", None)
    for t in (task, agent_task, lm_config_task):
        if t is None:
            continue
        if getattr(t, "done", lambda: False)():
            continue
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _release_owned_lm_studio_instance(app, raise_on_error=False),
        )
    except Exception:
        pass
    if getattr(app.state, "tool_hooks_installed", False):
        try:
            from clio_agent.tools.execution import (  # noqa: PLC0415
                set_global_cancellation_checker,
                set_global_permission_gate,
                set_global_tool_observer,
            )

            set_global_cancellation_checker(None)
            set_global_permission_gate(None)
            set_global_tool_observer(None)
        except Exception:  # pragma: no cover - defensive shutdown cleanup
            pass


async def _construct_agent_async(app: "FastAPI") -> None:
    """Build the real ClioAgent off the lifespan hot path.

    DSPy import + ARC hydration + expert wiring takes ~10 s on Aurora's
    frameworks Python (beartype import hook + Lustre cold reads). We
    run it via ``run_in_executor`` so the event loop stays free for
    /v1/capabilities, /v1/health, and the rest of the catalog while
    the agent constructs. On success, stamps ``app.state.agent`` +
    ``app.state.arc`` so the next POST /messages dispatches normally;
    on failure, logs and leaves ``agent=None`` so /messages keeps
    surfacing a structured 503 instead of a corrupted half-built
    agent.
    """

    loop = asyncio.get_running_loop()

    def _build() -> Any:
        import dspy  # noqa: PLC0415

        from clio_agent.agent import ClioAgent  # noqa: PLC0415
        from clio_agent.config import (  # noqa: PLC0415
            create_chat_adapter,
            create_lm,
            load_config_from_env,
        )

        cfg = load_config_from_env()
        dspy.configure(
            lm=create_lm(cfg),
            adapter=create_chat_adapter(cfg),
        )
        return ClioAgent(verbose=False)

    try:
        agent = await loop.run_in_executor(None, _build)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[clio-agent-gact] deferred agent init failed ({exc!r}); "
            "POST /messages will keep returning 503.",
            flush=True,
        )
        app.state.agent_init_error = repr(exc)
        return

    app.state.agent = agent
    app.state.arc = agent.arc

    # Install the deferred permission gate + tool observer now that we
    # know an agent exists to gate. See build_app for why these aren't
    # installed at construction time.
    try:
        from clio_agent.tools.execution import (  # noqa: PLC0415
            set_global_cancellation_checker,
            set_global_permission_gate,
            set_global_tool_observer,
        )

        checker = getattr(app.state, "pending_cancellation_checker", None)
        gate = getattr(app.state, "pending_permission_gate", None)
        observer = getattr(app.state, "pending_tool_observer", None)
        if checker is not None:
            set_global_cancellation_checker(checker)
        if gate is not None:
            set_global_permission_gate(gate)
        if observer is not None:
            set_global_tool_observer(observer)
        app.state.tool_hooks_installed = True
    except Exception:  # pragma: no cover - defensive
        pass

    print("[clio-agent-gact] agent ready.", flush=True)


async def _scheduler_tick(app: "FastAPI") -> None:
    """Once-a-minute loop: fire any due schedules.

    Each due schedule kicks the same _run_turn_in_background path
    a regular POST /messages would, so SSE subscribers see the
    automated turn unfold like any other.
    """

    while True:
        try:
            now = datetime.now(timezone.utc)
            for sch in list(app.state.schedules.due_now(now)):
                user_msg = Message(
                    id=_new_message_id("user"),
                    session_id=sch.session_id,
                    role="user",
                    created_at=_iso_from_epoch(time.time()),
                    updated_at=_iso_from_epoch(time.time()),
                    parts=[
                        Part(
                            id=_new_part_id(),
                            type="text",
                            text=sch.question,
                        )
                    ],
                    metadata={"scheduled": True, "schedule_id": sch.id},
                )
                _append_session_message(app, sch.session_id, user_msg)
                app.state.bus.publish(
                    Event(
                        type="message.created",
                        session_id=sch.session_id,
                        payload=user_msg.model_dump(exclude_none=True),
                    )
                )
                app.state.schedules.mark_fired(sch.id)
                # Fire-and-forget the turn task.
                asyncio.create_task(
                    _run_turn_in_background(
                        app,
                        sch.session_id,
                        sch.question,
                        user_msg,
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        # Sleep until just past the next minute boundary so we don't
        # double-fire on the same minute.
        await asyncio.sleep(60)


class ARCLike(Protocol):
    """Structural interface for the ARC reference /v1/memory/stats
    pulls from. Real ``ARCMemory`` matches it; tests pass a fake.

    ``get_cache_stats`` returns a dict with ``hits`` / ``misses`` /
    ``hit_rate`` / ``capacity`` (see ``ARCMemory.get_cache_stats``).
    """

    def get_cache_stats(self) -> dict[str, Any]:  # pragma: no cover
        ...


def build_app(
    sessions_path: Optional[Path] = None,
    agent: Optional[AgentLike] = None,
    arc: Optional[ARCLike] = None,
) -> FastAPI:
    """Construct the FastAPI app.

    Kept as a factory (not a module-level ``app = FastAPI()``) so
    tests can build fresh instances without singleton state; the
    module-level ``app`` below is for ``uvicorn
    clio_agent.gact.app:app`` invocations.

    ``sessions_path`` overrides where the session registry persists.
    ``None`` uses the production default (``~/.config/clio-agent/
    sessions.json``); tests pass ``tmp_path / "sessions.json"`` for
    isolation.

    ``agent`` is the ClioAgent-like object driving turns. Left
    ``None`` for builds that only exercise session CRUD without
    actual LM calls — endpoints needing an agent (POST messages, SSE)
    return a structured 503 until one is wired. Production main()
    constructs a real ``ClioAgent`` and passes it here.
    """

    app = FastAPI(
        title="CLIO GACT v0.2",
        version=GACT_BACKEND_VERSION,
        lifespan=_lifespan,
    )
    # Initialise state eagerly in case the caller skips the lifespan
    # context (TestClient normally runs it, but older FastAPI + some
    # test-utility paths don't).
    app.state.started_at = time.time()
    session_store_path = sessions_path if sessions_path is not None else _default_store_path()
    app.state.sessions = SessionStore(path=session_store_path)
    app.state.agent = agent  # may be None; POST message checks before using
    app.state.arc = arc  # may be None; /v1/memory/stats returns zeros in that case
    prompt_write_root = session_store_path.parent / "prompts"
    app.state.prompt_registry = PromptRegistry(
        sources=[
            PromptSource("global", prompt_write_root),
            PromptSource("workspace", Path.cwd() / ".clio" / "prompts"),
        ],
        write_root=prompt_write_root,
    )
    app.state.memory_events = {}
    # CLIO-BBBBBBBBBB13: per-session pub/sub. POST /messages
    # publishes; /v1/sessions/{sid}/events subscribers consume.
    app.state.bus = EventBus()
    # CLIO-BBBBBBBBBB14: message log keyed by session_id. Populated by
    # POST /messages, read by GET /messages, and backed by per-session
    # JSON ledgers so adapter deletion/redeploy preserves transcripts.
    app.state.message_store = MessageStore(path=session_store_path.parent / "messages")
    app.state.messages = app.state.message_store.load_all()
    # CLIO-BBBBBBBBBB20: cooperative cancellation flags. POST /cancel
    # adds a sid; the POST-message handler checks + clears after the
    # agent returns. Set (not dict) because the flag's presence IS
    # the signal — no payload.
    app.state.cancel_flags = set()
    app.state.cancel_events = {}
    app.state.cancel_attempts = {}
    # CLIO-BBBBBBBBBB22: per-session context files. Keyed by
    # session_id, each value is an ordered dict of
    # path -> ContextFile dict.
    app.state.context_files_path = session_store_path.parent / "context_files.json"
    app.state.context_files = _load_context_files(app.state.context_files_path)
    # iowarp/clio-agent#331: per-turn context truth frames. These
    # capture what visible transcript/context attachments were
    # retained for a turn, plus model/agent/prompt provenance.
    app.state.context_frames = {}
    # CLIO-BBBBBBBBBB21: per-session pending diffs. Keyed by
    # session_id -> list of {path, unified_diff, status,
    # part_id, message_id}. Status is "pending" until apply/reject
    # flips it.
    app.state.pending_diffs = {}
    # CLIO-BBBBBBBBBB23: pending permission requests. Flat dict
    # keyed by permission_id so GET /v1/permissions can filter by
    # session cheaply. Each record carries
    # {id, session_id, tool_call, summary, created_at, status,
    #  action, resolved_at}.
    app.state.permissions = {}
    # iowarp/clio-agent#7: per-permission threading.Event so the
    # MCPToolBridge gate (running in a worker thread) can block on
    # the user's response without polling.
    app.state.permission_events = {}
    # iowarp/clio-agent#333: structured ask-user protocol. The
    # orchestrator/backend can publish pending questions; clients
    # answer or cancel them through explicit endpoints.
    app.state.user_questions = {}
    # iowarp/clio-agent#333: retry attempts preserve provenance for
    # retry-with-notes/model flows without mutating the original turn.
    app.state.turn_attempts = {}
    # SPEC §6.17 hooks (declarative event→command/url callouts that
    # gact-tui drives via /v1/hooks). Distinct from CLIO's runtime
    # in-process Python hooks (clio_agent.runtime.hooks) — these are
    # user-configurable callouts the agent fires during the turn
    # lifecycle, while the Python runtime hooks are framework-level
    # extension points. In-memory; not persisted across restarts.
    app.state.declarative_hooks = {}
    # SPEC §6.11.b permission policies — list, not dict. Backends
    # consult this on every tool call to decide allow/deny/ask before
    # falling back to the per-tool permission_default. PUT replaces
    # the whole list.
    app.state.permission_policies_path = session_store_path.parent / "permission_policies.json"
    app.state.permission_policies = _load_permission_policies(app.state.permission_policies_path)
    # iowarp/clio-agent#18: per-session task list (todo-style).
    # Keyed by session_id -> {task_id -> task dict}. In-memory.
    app.state.session_tasks = {}
    # iowarp/clio-agent#3: per-session in-flight turn tasks. POST
    # /messages tracks the asyncio.Task here so /cancel can
    # hard-abort instead of waiting for the cooperative flag check.
    app.state.in_flight_turns = {}
    # iowarp/clio-agent#2: per-session ledger of tool calls observed
    # during the in-flight turn. The global tool_observer appends
    # here; _run_turn_in_background drains it post-forward to attach
    # tools_called metadata even when the underlying expert
    # didn't populate ``pred.tools_called`` itself.
    app.state.tool_call_ledger = {}

    # iowarp/clio-agent#7 + #2: install process-global hooks on the
    # MCPToolBridge so EVERY expert's tool call routes through our
    # permission gate + telemetry observer.
    #
    # When an agent is already in hand we install eagerly — that's
    # the legacy build_app(agent=X) path tests use. When the caller
    # left agent=None (the production main() flow that defers
    # ClioAgent construction to the lifespan task) we stash the
    # closures on app.state and install them right after the agent
    # finishes constructing — importing clio_agent.tools.execution
    # transitively pulls litellm + dspy (~4 s) and we need build_app
    # to stay cheap enough for gact-tui's 3-second deploy probe.
    if agent is not None:
        try:
            from clio_agent.tools.execution import (
                set_global_cancellation_checker,
                set_global_permission_gate,
                set_global_tool_observer,
            )

            set_global_cancellation_checker(_make_cancellation_checker(app))
            set_global_permission_gate(_make_permission_gate(app))
            set_global_tool_observer(_make_tool_observer(app))
            app.state.tool_hooks_installed = True
        except Exception:  # pragma: no cover - defensive
            pass
    else:
        app.state.tool_hooks_installed = False
        app.state.pending_cancellation_checker = _make_cancellation_checker(app)
        app.state.pending_permission_gate = _make_permission_gate(app)
        app.state.pending_tool_observer = _make_tool_observer(app)

    # iowarp/clio-agent#20: install the user-hooks registry so
    # pre_tool / post_tool / pre_message / post_message events
    # route to ~/.config/clio-agent/hooks/<event>.py. Tests pre-
    # install their own registry; we only install a default if
    # nothing's currently wired so the test-side hook stays.
    try:
        from clio_agent.runtime.hooks import (
            HookRegistry,
            install_global_registry,
        )
        from clio_agent.runtime.hooks import (
            _registry as _current_registry,
        )

        if _current_registry is None:
            install_global_registry(HookRegistry())
    except Exception:  # pragma: no cover - defensive
        pass

    # CLIO-BBBBBBBBBB-D: live LM config — what the TUI configured
    # us with. Distinct from boot-time env because PUT /providers/lm
    # rebuilds the agent + DSPy config in-place.
    app.state.lm_config = None
    app.state.lm_config_status = {"state": "idle"}
    app.state.lm_config_task = None
    app.state.lm_studio_owned_instance = None
    # CLIO-BBBBBBBBBB-WS: workspaces store. Persisted alongside
    # sessions; seeds a default workspace if none exist so the TUI
    # always has something to render.
    app.state.workspaces = WorkspaceStore(
        path=(sessions_path.parent / "workspaces.json")
        if sessions_path is not None
        else _ws_default_store_path()
    )
    # iowarp/clio-agent#19: dynamic agent registry. Persists user-
    # registered Tier-2 specialists alongside sessions/workspaces;
    # built-ins always take precedence on id clash (rejected at
    # the HTTP layer).
    from clio_agent.gact.user_agents import (
        UserAgentStore,
    )
    from clio_agent.gact.user_agents import (
        _default_store_path as _ua_default,
    )

    app.state.user_agents = UserAgentStore(
        path=(sessions_path.parent / "agents.json") if sessions_path is not None else _ua_default()
    )
    # iowarp/clio-agent#21: scheduled turns store + tick task.
    from clio_agent.gact.scheduler import ScheduleStore as _SchedStore

    app.state.schedules = _SchedStore(
        path=(sessions_path.parent / "schedules.json") if sessions_path is not None else None
    )
    app.state.scheduler_task = None
    # iowarp/clio-agent#22: shared session tokens.
    app.state.shared_tokens = {}

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse | JSONResponse:
        """SPEC §3.4 — per-subsystem status feeds the TUI's /doctor
        modal (v0.2 `integration_health`). We report on whatever is
        actually wired in this build: the API itself, the session
        store, the agent (real vs fake vs not-wired), and ARC.

        overall_status collapses the rows to the worst case:
        ready > degraded > unavailable.
        """

        uptime = int(time.time() - app.state.started_at)
        rows: list[Integration] = [
            Integration(
                name="api",
                status="ready",
                detail=f"clio-agent-gact {GACT_BACKEND_VERSION}",
            ),
            Integration(
                name="sessions",
                status="ready",
                detail=f"{len(app.state.sessions.list())} session(s) registered",
            ),
        ]

        agent = app.state.agent
        if agent is None:
            rows.append(
                Integration(
                    name="agent",
                    status="unavailable",
                    detail="no ClioAgent wired; POST /messages will 503",
                )
            )
        else:
            # Heuristic: the production ClioAgent is a class that
            # imports DSPy under the hood and exposes it via
            # `agent.__class__.__module__`. The smoke/test fakes
            # live under 'gact_smoke_server' or '__main__'. Label
            # them so the /doctor modal is honest about what's
            # running.
            mod = type(agent).__module__
            is_fake = "smoke" in mod or mod == "__main__" or "test" in mod.lower()
            rows.append(
                Integration(
                    name="agent",
                    status="degraded" if is_fake else "ready",
                    detail=(
                        f"{type(agent).__name__} (fake — dev harness)"
                        if is_fake
                        else f"{type(agent).__name__} wired"
                    ),
                )
            )

        if app.state.arc is None:
            rows.append(
                Integration(
                    name="memory",
                    status="degraded",
                    detail="memory layer not wired; /v1/memory/stats returns zeros",
                )
            )
        else:
            try:
                stats = app.state.arc.get_cache_stats()
                hr = stats.get("hit_rate", 0.0)
                rows.append(
                    Integration(
                        name="memory",
                        status="ready",
                        detail=f"cache {int(hr * 100)}% hit rate",
                    )
                )
            except Exception as exc:
                rows.append(
                    Integration(
                        name="memory",
                        status="unavailable",
                        detail=f"memory cache stats raised: {exc!r}",
                    )
                )

        # LM row drives the TUI's "configure provider on connect"
        # decision. ``configured`` mirrors what GET /v1/providers/lm
        # reports — agent present + last-known config from PUT.
        cfg = _effective_lm_config(app)
        lm_config_status = getattr(app.state, "lm_config_status", {}) or {}
        if lm_config_status.get("state") == "configuring":
            rows.append(
                Integration(
                    name="lm",
                    status="degraded",
                    detail=(
                        "configuring "
                        f"{lm_config_status.get('provider', '?')}/"
                        f"{lm_config_status.get('model', '?')}"
                    ),
                )
            )
        elif lm_config_status.get("state") == "error":
            rows.append(
                Integration(
                    name="lm",
                    status="unavailable",
                    detail=str(
                        lm_config_status.get("message") or "LM provider configuration failed"
                    ),
                )
            )
        elif app.state.agent is not None and cfg:
            detail = f"{cfg.get('provider', '?')}/{cfg.get('model', '?')}"
            lm_status: Literal["ready", "degraded", "unavailable"] = "ready"
            if cfg.get("provider") == "argonne":
                try:
                    from clio_agent.providers import argonne_auth  # noqa: PLC0415

                    if not argonne_auth.tokens_exist():
                        lm_status = "unavailable"
                        detail += " (ALCF Globus token missing)"
                    else:
                        lm_status = "degraded"
                        detail += " (ALCF Globus token stored; validate before use)"
                except Exception as exc:
                    lm_status = "unavailable"
                    detail += f" (ALCF auth check failed: {exc})"
            rows.append(
                Integration(
                    name="lm",
                    status=lm_status,
                    detail=detail,
                )
            )
        elif app.state.agent is not None:
            # Agent wired by env at boot; lm_config wasn't recorded
            # but we know an LM is configured.
            rows.append(
                Integration(
                    name="lm",
                    status="ready",
                    detail="configured from env at boot",
                )
            )
        else:
            rows.append(
                Integration(
                    name="lm",
                    status="unavailable",
                    detail=(
                        "no LM configured; PUT /v1/providers/lm or set CLIO_LM_PROVIDER and restart"
                    ),
                )
            )

        # Worst-status wins.
        statuses = {r.status for r in rows}
        if "unavailable" in statuses:
            overall = "unavailable"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "ready"

        response = HealthResponse(
            healthy=overall != "unavailable",
            uptime_s=uptime,
            overall_status=overall,  # type: ignore[arg-type]  # narrowed by branches above
            integrations=rows,
        )
        if overall == "unavailable":
            return JSONResponse(
                status_code=503,
                content=response.model_dump(mode="json", exclude_none=True),
            )
        return response

    @app.get("/v1/capabilities", response_model=Capabilities)
    async def capabilities() -> Capabilities:
        return Capabilities(
            contract_version=CONTRACT_VERSION,
            backend=BackendInfo(
                name="clio-agent-gact",
                version=GACT_BACKEND_VERSION,
                vendor="iowarp",
                homepage="https://github.com/iowarp/clio-agent",
            ),
            capabilities=CapabilityFlags(
                # v0.1 baseline — flipped on as each surface lands.
                # Honest reporting lets the TUI disable UI for
                # capabilities we don't actually provide.
                sessions=True,  # BBB8 — /v1/sessions CRUD
                workspaces=True,  # CLIO-WS — /v1/workspaces CRUD
                metrics=True,  # BBB15 — /v1/metrics returns SPEC §6.16 envelope
                session_branching=True,  # BBB26 — POST /sessions/{sid}/fork
                search_messages=True,  # BBB27 — GET /sessions/{sid}/messages/search
                cost_tracking=True,  # BBB24 — Message.tokens + Session.cost_usd rollup
                files=True,  # BBB22 — /v1/sessions/{sid}/context/files CRUD
                diffs=True,  # BBB21 — file_diff parts + /diffs/apply,reject
                permissions=True,  # BBB23 — /v1/permissions + permission.* events
                subagents=True,  # BBB25 — nanoagent subsessions + subagent.* events
                session_export=True,  # #16 — /v1/sessions/{sid}/export + import
                mcp=True,  # #13 — /v1/mcp/servers exposes the gateway namespaces
                providers=True,  # #15 — /v1/providers catalogs the LM presets
                commands=True,  # #14 — /v1/commands + dispatch
                thinking_blocks=True,  # #17 — DSPy reasoning trace as thinking Parts
                session_tasks=True,  # #18 — per-session todo CRUD
                plan_mode=True,  # session.mode=plan blocks destructive tools
                edit_modes=True,  # session.edit_mode toggles diff/whole/patch
                agent_write=True,  # #19 — POST/PUT/DELETE /v1/agents
                hooks=True,  # #20 — pre/post_tool + pre/post_message hooks
                scheduled_sessions=True,  # #21 — cron schedules
                session_sharing=True,  # #22 — share tokens
                skills_extraction=True,  # #23 — POST /v1/agents/extract
                # v0.2 additions — advertised when the scaffold
                # actually emits them. Turned on piecewise as the
                # follow-on items land.
                agent_routing=True,  # BBB10 — /v1/agents?tier= + tier-2 catalog
                memory=True,  # BBB11 — /v1/memory/stats backed by ARC
                structured_errors=True,  # always — we return the envelope for every error
                integration_health=True,  # /v1/health above carries it
                tool_telemetry=True,  # BBB18 — tool.call.started/completed events
                x_clio_cancellation="best_effort",
                x_clio_executor_cancellation=False,
                x_clio_text_streaming="best_effort_live",
                x_clio_synthetic_posthoc_streaming=False,
                x_clio_stream_fallback_reasons=_stream_fallback_reason_capabilities(),
                x_clio_direct_delete_permissions=True,
                x_clio_prompt_registry=True,
                x_clio_expert_packs=True,
                x_clio_user_questions=True,
                x_clio_retry_attempts=True,
                x_clio_context_frames=True,
                x_clio_capability_gaps=_capability_gap_metadata(),
            ),
            transports=TransportFlags(events_sse=True, events_websocket=False),
            auth=AuthInfo(schemes=["trust_socket"], current="trust_socket"),
        )

    @app.get("/v1/capability-gaps")
    async def capability_gaps() -> dict[str, Any]:
        """Return intentionally unsupported or future CLIO capability rows.

        This keeps "not supported yet" affordances visible as ideas without
        making clients infer support from missing routes or failed commands.
        """

        return {"capability_gaps": _capability_gap_metadata()}

    # ---- 501 stubs for the rest of the surface ---------------------------
    # Every route in the v0.2 contract that we haven't wired yet
    # returns the structured error envelope from above. Matches the
    # shape v0.2 clients expect, while honestly reporting that the
    # backend doesn't yet implement the endpoint.

    # ---- /v1/prompts (CLIO prompt registry) ------------------------------

    @app.get("/v1/prompts")
    async def list_prompts() -> dict[str, Any]:
        """List built-in and external prompt definitions.

        This is a CLIO vendor surface rather than a core GACT v0.2 route. The
        TUI can use it later to browse prompts, profiles, validation state, and
        provenance without knowing where prompt files live on disk.
        """

        rows = app.state.prompt_registry.list()
        return {"prompts": [asdict(row) for row in rows]}

    @app.get("/v1/prompts/{prompt_id:path}")
    async def get_prompt(prompt_id: str, profile: str = "") -> dict[str, Any]:
        resolved = app.state.prompt_registry.resolve(prompt_id, profile=profile)
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"prompt not found: {prompt_id}",
                        details={"prompt_id": prompt_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"prompt": asdict(resolved)}

    @app.post("/v1/prompts/{prompt_id:path}/render")
    async def render_prompt(prompt_id: str, request: Request, profile: str = "") -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        requested_profile = str(body.get("profile") or profile or "")
        context_override = body.get("context")
        context = _prompt_render_context(app)
        if isinstance(context_override, Mapping):
            for key, value in context_override.items():
                context[str(key)] = str(value)
        rendered = app.state.prompt_registry.render(
            prompt_id,
            profile=requested_profile,
            context=context,
        )
        if rendered is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"prompt not found: {prompt_id}",
                        details={"prompt_id": prompt_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"prompt": asdict(rendered)}

    @app.put("/v1/prompts/{prompt_id:path}")
    async def save_prompt(prompt_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        text = str(body.get("text") or "")
        if not text.strip():
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="missing required field: text",
                        details={"prompt_id": prompt_id, "field": "text"},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            row = app.state.prompt_registry.save(
                prompt_id,
                text=text,
                profile=str(body.get("profile") or "default"),
                title=str(body.get("title") or ""),
                description=str(body.get("description") or ""),
                provider=str(body.get("provider") or ""),
                model=str(body.get("model") or ""),
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=str(exc),
                        details={"prompt_id": prompt_id},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        return {"prompt": asdict(row)}

    # ---- /v1/sessions CRUD -----------------------------------------
    # CLIO-BBBBBBBBBB8 — four real handlers against app.state.sessions
    # (the SessionStore wired above). Kept as nested closures so they
    # can close over `app` cleanly without passing the store around.

    @app.post("/v1/sessions", response_model=Session)
    async def create_session(req: CreateSessionRequest) -> Session:
        wid = req.workspace_id or "ws_default"
        if app.state.workspaces.get(wid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        sess = app.state.sessions.create(
            workspace_id=wid,
            title=req.title,
            metadata=req.metadata,
            model=req.model.model_dump(exclude_none=True) if req.model else None,
            agent=req.agent.model_dump(exclude_none=True) if req.agent else None,
            mode=req.mode,
            edit_mode=req.edit_mode,
            routing_mode=req.routing_mode,
        )
        _mirror_workspace_session(app, sess.id)
        return Session(**sess.to_wire())

    @app.patch("/v1/sessions/{sid}", response_model=Session)
    async def patch_session(sid: str, req: UpdateSessionRequest) -> Session:
        """Update mutable session fields (title + mode + edit_mode).

        Lets the TUI flip plan ↔ edit ↔ chat ↔ architect mid-
        session without recreating, and rename via the existing
        rename modal.
        """

        sess = app.state.sessions.update(
            sid,
            title=req.title,
            mode=req.mode,
            edit_mode=req.edit_mode,
            routing_mode=req.routing_mode,
            model=req.model.model_dump(exclude_none=True) if req.model else None,
            agent=req.agent.model_dump(exclude_none=True) if req.agent else None,
        )
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Publish so live SSE subscribers see mode flips immediately.
        app.state.bus.publish(
            Event(
                type="session.updated",
                session_id=sid,
                payload=Session(**sess.to_wire()).model_dump(exclude_none=True),
            )
        )
        _mirror_workspace_session(app, sid)
        return Session(**sess.to_wire())

    @app.get("/v1/sessions", response_model=ListSessionsResponse)
    async def list_sessions(workspace_id: Optional[str] = None) -> ListSessionsResponse:
        rows = app.state.sessions.list(workspace_id=workspace_id)
        return ListSessionsResponse(sessions=[Session(**row.to_wire()) for row in rows])

    @app.get("/v1/sessions/{sid}", response_model=Session)
    async def get_session(sid: str) -> Session:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Session(**sess.to_wire())

    @app.get("/v1/sessions/{sid}/context/policy", response_model=SessionContextPolicy)
    async def get_session_context_policy(sid: str) -> SessionContextPolicy:
        """Return CLIO's effective context compartment policy for one session."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        ws = app.state.workspaces.get(sess.workspace_id)
        scope_meta = workspace_scope(ws).to_wire() if ws is not None else {}
        return SessionContextPolicy(
            session_id=sid,
            notes=[
                "Conversation retrieval and writes are scoped to the active session.",
                "Cross-session memory search is not exposed by this endpoint yet.",
                "A future explicit tool may allow consented cross-session reads.",
            ],
            metadata={
                "source": "clio_backend_default",
                "session_mode": sess.mode,
                "routing_mode": sess.routing_mode,
                "arc_wired": app.state.arc is not None,
                "workspace": scope_meta,
            },
        )

    @app.delete("/v1/sessions/{sid}")
    async def delete_session(sid: str) -> Response:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        _guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.delete",
            args={"session_id": sid},
            summary=f"delete session {sid}",
            reason="user_requested_session_delete",
        )
        _remove_workspace_session_mirror(app, sid)
        existed = app.state.sessions.delete(sid)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        _delete_session_messages(app, sid)
        _delete_session_context_files(app, sid)
        return Response(status_code=204)

    def _reject_rollback_while_active(sid: str, sess: Any) -> None:
        if getattr(sess, "status", "") in {"running", "waiting_permission"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="conflict",
                        message=f"session {sid} cannot be rolled back while {sess.status}",
                        details={"session_id": sid, "status": sess.status},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

    def _publish_rollback_events(
        sid: str,
        *,
        operation: str,
        deleted_ids: list[str],
        session_payload: dict[str, Any],
        target_message_id: str = "",
        include_target: bool = False,
    ) -> None:
        for message_id in deleted_ids:
            app.state.bus.publish(
                Event(
                    type="message.deleted",
                    session_id=sid,
                    payload={
                        "message_id": message_id,
                        "session_id": sid,
                        "operation": operation,
                    },
                )
            )
        app.state.bus.publish(
            Event(
                type=f"session.{operation}",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "deleted_message_ids": deleted_ids,
                    "target_message_id": target_message_id,
                    "include_target": include_target,
                },
            )
        )
        app.state.bus.publish(
            Event(
                type="session.updated",
                session_id=sid,
                payload=session_payload,
            )
        )

    def _commit_rollback(
        sid: str,
        *,
        operation: str,
        kept_messages: list[Message],
        deleted_messages: list[Message],
        target_message_id: str = "",
        include_target: bool = False,
    ) -> dict[str, Any]:
        _replace_session_messages(app, sid, kept_messages)
        deleted_ids = [m.id for m in deleted_messages]
        updated = app.state.sessions.update(
            sid,
            message_count=len(kept_messages),
            status="idle",
            metadata_patch={
                "last_rollback": {
                    "operation": operation,
                    "deleted_message_ids": deleted_ids,
                    "target_message_id": target_message_id,
                    "include_target": include_target,
                    "memory_scope": "gact_visible_transcript_only",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        if updated is None:
            raise _session_not_found(sid)
        session_payload = Session(**updated.to_wire()).model_dump(exclude_none=True)
        _publish_rollback_events(
            sid,
            operation=operation,
            deleted_ids=deleted_ids,
            session_payload=session_payload,
            target_message_id=target_message_id,
            include_target=include_target,
        )
        return {
            "session_id": sid,
            "operation": operation,
            "deleted_message_ids": deleted_ids,
            "deleted_messages": deleted_ids,
            "reverted_message_ids": deleted_ids,
            "message_count": len(kept_messages),
            "memory_scope": "gact_visible_transcript_only",
            "session": session_payload,
        }

    @app.post("/v1/sessions/{sid}/undo")
    async def undo_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        _reject_rollback_while_active(sid, sess)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="undo request body must be an object",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        raw_count = body.get("count", body.get("message_count", 1))
        try:
            count = int(raw_count) if isinstance(raw_count, str | int | float) else 1
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="undo count must be at least 1",
                        details={"session_id": sid, "count": raw_count},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        messages = list(app.state.messages.get(sid, []))
        deleted = messages[-count:]
        kept = messages[: max(0, len(messages) - count)]
        _guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.undo",
            args={"session_id": sid, "count": count},
            summary=f"undo last {count} message(s) in session {sid}",
            reason="user_requested_session_undo",
        )
        return _commit_rollback(
            sid,
            operation="undo",
            kept_messages=kept,
            deleted_messages=deleted,
        )

    @app.post("/v1/sessions/{sid}/rewind")
    async def rewind_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        _reject_rollback_while_active(sid, sess)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="rewind request body must be an object",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        target_message_id = str(
            body.get("message_id")
            or body.get("target_message_id")
            or body.get("to_message_id")
            or ""
        ).strip()
        if not target_message_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="rewind requires message_id",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        include_target = bool(body.get("include_target", False))
        messages = list(app.state.messages.get(sid, []))
        target_index = next(
            (index for index, message in enumerate(messages) if message.id == target_message_id),
            -1,
        )
        if target_index < 0:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found: {target_message_id}",
                        details={"session_id": sid, "message_id": target_message_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        keep_end = target_index if include_target else target_index + 1
        kept = messages[:keep_end]
        deleted = messages[keep_end:]
        _guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.rewind",
            args={
                "session_id": sid,
                "message_id": target_message_id,
                "include_target": include_target,
            },
            summary=f"rewind session {sid} to message {target_message_id}",
            reason="user_requested_session_rewind",
        )
        return _commit_rollback(
            sid,
            operation="rewind",
            kept_messages=kept,
            deleted_messages=deleted,
            target_message_id=target_message_id,
            include_target=include_target,
        )

    # ---- /v1/permissions (BBB23) --------------------------------------

    @app.get("/v1/permissions")
    async def list_permissions(
        session_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """List permission requests.

        ?session_id=<sid> narrows to a session; ?status=pending
        hides resolved rows; ?status=all returns the audit ledger.
        """

        rows = list(app.state.permissions.values())
        total_before_filters = len(rows)
        if session_id:
            rows = [r for r in rows if r.get("session_id") == session_id]
        total_after_session_filter = len(rows)
        if status and status != "all":
            rows = [r for r in rows if r.get("status") == status]
        total_after_status_filter = len(rows)
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        if limit <= 0:
            limit = 100
        limit = min(limit, 500)
        return {
            "permissions": rows[:limit],
            "metadata": {
                "session_id": session_id,
                "status": status or "all",
                "limit": limit,
                "total": total_after_status_filter,
                "returned": min(total_after_status_filter, limit),
                "truncated": total_after_status_filter > limit,
                "total_before_filters": total_before_filters,
                "total_after_session_filter": total_after_session_filter,
            },
        }

    @app.post("/v1/permissions/{pid}")
    async def respond_permission(pid: str, request: Request) -> Response:
        """Resolve a pending permission. Body: ``{action}`` where
        action is ``allow | deny | allow_session | allow_workspace``.
        Idempotent when the row is already resolved (returns the
        existing resolution rather than erroring).
        """

        row = app.state.permissions.get(pid)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"permission not found: {pid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        action = body.get("action") or ""
        if action not in {"allow", "deny", "allow_session", "allow_workspace"}:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(
                            "action must be one of allow, deny, allow_session, allow_workspace"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if row.get("status") == "pending":
            row["status"] = "resolved"
            row["action"] = action
            row["resolved_at"] = datetime.now(timezone.utc).isoformat()
            policy = _append_permission_policy_from_resolution(app, row=row, action=action)
            if policy is not None:
                row["policy"] = policy
            # iowarp/clio-agent#7: wake any MCPToolBridge thread
            # waiting on this permission's event.
            evt = app.state.permission_events.pop(pid, None)
            if evt is not None:
                evt.set()
            app.state.bus.publish(
                Event(
                    type="permission.resolved",
                    session_id=row.get("session_id", ""),
                    payload={
                        "permission_id": pid,
                        "action": action,
                        "session_id": row.get("session_id", ""),
                    },
                )
            )
        return Response(status_code=204)

    # ---- /v1/sessions/{sid}/diffs/* (BBB21) ---------------------------

    def _filter_diff_paths(rows: list[dict[str, Any]], paths: list[str]) -> list[dict[str, Any]]:
        """Narrow pending diffs to a given path allow-list. Empty
        list (or no param) means "every pending row"."""

        if not paths:
            return [r for r in rows if r["status"] == "pending"]
        allow = set(paths)
        return [r for r in rows if r["path"] in allow and r["status"] == "pending"]

    def _diff_row_to_wire(row: dict[str, Any]) -> dict[str, Any]:
        """Convert an internal pending-diff row to the GACT file_diff shape."""

        status = str(row.get("status") or "pending")
        out: dict[str, Any] = {
            "path": row.get("path", ""),
            "applied": status == "applied",
            "status": status,
        }
        if row.get("unified_diff") is not None:
            out["unified_diff"] = row.get("unified_diff")
        if row.get("part_id"):
            out["part_id"] = row.get("part_id")
        if row.get("message_id"):
            out["message_id"] = row.get("message_id")
        return out

    @app.get("/v1/sessions/{sid}/diffs")
    async def list_session_diffs(sid: str) -> dict[str, Any]:
        """SPEC §6.6/§6.9 read endpoint for pending/applied file diffs."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"diffs": [_diff_row_to_wire(row) for row in app.state.pending_diffs.get(sid, [])]}

    @app.get("/v1/sessions/{sid}/messages/{message_id}/diffs")
    async def list_message_diffs(sid: str, message_id: str) -> dict[str, Any]:
        """Return file diffs associated with a specific assistant message."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if not any(m.id == message_id for m in app.state.messages.get(sid, [])):
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found: {message_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {
            "diffs": [
                _diff_row_to_wire(row)
                for row in app.state.pending_diffs.get(sid, [])
                if row.get("message_id") == message_id
            ]
        }

    @app.post("/v1/sessions/{sid}/diffs/apply")
    async def diffs_apply(sid: str, request: Request) -> dict[str, Any]:
        """Mark pending diffs as applied + actually write to disk
        via the fs_apply_edit_write MCP tool.

        Body: ``{paths: [...]}`` (optional). If omitted, every
        pending diff is applied. Returns ``{applied: [...],
        write_errors?: {...}}``. iowarp/clio-agent#4: writes are
        scoped to the session's workspace.root_path; failures
        per-path go into write_errors but don't block the rest.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]

        rows = app.state.pending_diffs.get(sid, [])
        targets = _filter_diff_paths(rows, paths)
        applied: list[str] = []
        write_errors: dict[str, str] = {}
        for r in targets:
            # iowarp/clio-agent#4: actually write to disk if the
            # row carries a `new_content` field. The
            # propose_edit-driven path always sets it; legacy/test
            # diffs that don't get the wire event but no write.
            new_content = r.get("new_content")
            if new_content is not None:
                try:
                    _apply_edit_to_disk(
                        path=r["path"],
                        new_content=new_content,
                        session=sess,
                        app=app,
                    )
                except Exception as exc:  # noqa: BLE001
                    err = repr(exc)
                    write_errors[r["path"]] = err
                    r["status"] = "apply_failed"
                    # Publish a failure event so the TUI sees the write
                    # error live (was a silent failure: the response
                    # body carried write_errors but the TUI's apply-
                    # button path discards it). file.diff.write_failed
                    # mirrors file.diff.applied for parity.
                    app.state.bus.publish(
                        Event(
                            type="file.diff.write_failed",
                            session_id=sid,
                            payload={
                                "session_id": sid,
                                "path": r["path"],
                                "part_id": r.get("part_id", ""),
                                "message_id": r.get("message_id", ""),
                                "error": err,
                            },
                        )
                    )
                    continue
            r["status"] = "applied"
            applied.append(r["path"])
            app.state.bus.publish(
                Event(
                    type="file.diff.applied",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "path": r["path"],
                        "part_id": r.get("part_id", ""),
                        "message_id": r.get("message_id", ""),
                    },
                )
            )
        out: dict[str, Any] = {"applied": applied}
        if write_errors:
            out["write_errors"] = write_errors
        return out

    @app.post("/v1/sessions/{sid}/diffs/reject")
    async def diffs_reject(sid: str, request: Request) -> dict[str, list[str]]:
        """Mark pending diffs as rejected + publish events."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]

        rows = app.state.pending_diffs.get(sid, [])
        targets = _filter_diff_paths(rows, paths)
        rejected: list[str] = []
        for r in targets:
            r["status"] = "rejected"
            rejected.append(r["path"])
            app.state.bus.publish(
                Event(
                    type="file.diff.rejected",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "path": r["path"],
                        "part_id": r.get("part_id", ""),
                        "message_id": r.get("message_id", ""),
                    },
                )
            )
        return {"rejected": rejected}

    # ---- /v1/sessions/{sid}/context/files (BBB22) ---------------------

    def _resolve_context_attachment_path(
        *,
        sess: Any,
        raw_path: str,
        requested_workspace_id: str = "",
    ) -> dict[str, Any]:
        source = "mention" if raw_path.startswith("@") else "api"
        attachment_path = raw_path[1:].strip() if raw_path.startswith("@") else raw_path
        if not attachment_path:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: path",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        workspace_id = requested_workspace_id or getattr(sess, "workspace_id", "") or "ws_default"
        path_obj = Path(attachment_path).expanduser()
        if path_obj.is_absolute():
            try:
                resolved = path_obj.resolve(strict=False)
            except (OSError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="bad_request",
                            message=f"invalid context file path: {raw_path}",
                            details={"field": "path", "original_error": type(exc).__name__},
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc
            return {
                "path": str(resolved),
                "display_path": attachment_path,
                "resolved_path": str(resolved),
                "workspace_id": workspace_id,
                "source": source,
            }

        ws = app.state.workspaces.get(workspace_id)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {workspace_id}",
                        details={"workspace_id": workspace_id},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            root = Path(ws.root_path or os.getcwd()).expanduser().resolve()
            resolved = (root / attachment_path).resolve(strict=False)
            resolved.relative_to(root)
        except ValueError:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="path_outside_workspace",
                        message=f"context path escapes workspace: {raw_path}",
                        details={"path": raw_path, "workspace_id": workspace_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=f"invalid context file path: {raw_path}",
                        details={"field": "path", "original_error": type(exc).__name__},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        display_path = attachment_path.replace("\\", "/")
        return {
            "path": display_path,
            "display_path": display_path,
            "resolved_path": str(resolved),
            "workspace_id": workspace_id,
            "source": source,
        }

    @app.get("/v1/sessions/{sid}/context/frames")
    async def list_context_frames(sid: str, limit: int = 50) -> dict[str, Any]:
        """List per-turn context truth frames for a session."""

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        limit = max(1, min(int(limit or 50), 200))
        rows = list(app.state.context_frames.get(sid, []))
        return {"frames": rows[-limit:]}

    @app.get("/v1/sessions/{sid}/context/frames/{frame_id}")
    async def get_context_frame(sid: str, frame_id: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        for row in app.state.context_frames.get(sid, []):
            if row.get("id") == frame_id:
                return {"frame": row}
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"context frame not found: {frame_id}",
                    details={"session_id": sid, "frame_id": frame_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.get("/v1/sessions/{sid}/context/files")
    async def list_context_files(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = list(app.state.context_files.get(sid, {}).values())
        return {"files": rows}

    @app.post("/v1/sessions/{sid}/context/files")
    async def add_context_file(sid: str, request: Request) -> dict[str, Any]:
        """Attach a file to the session's context. Body: ``{path,
        mode?, size?, last_modified?, language?}``. Existing rows
        for the same path are upserted so the TUI can swap modes
        without racing an explicit delete.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = (body.get("path") or "").strip()
        resolved_info = _resolve_context_attachment_path(
            sess=sess,
            raw_path=path,
            requested_workspace_id=str(body.get("workspace_id") or ""),
        )
        mode = body.get("mode") or "read"
        if mode not in {"edit", "read", "pin"}:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=(
                            f"invalid context file mode: {mode!r}; expected edit, read, or pin"
                        ),
                        details={"field": "mode", "allowed": ["edit", "read", "pin"]},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        resolved = Path(resolved_info["resolved_path"])
        if mode in {"read", "pin"}:
            if not resolved.exists():
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="context_file_error",
                            message=f"context file not found: {path}",
                            details={
                                "path": path,
                                "resolved_path": str(resolved),
                                "display_path": resolved_info.get("display_path") or path,
                                "workspace_id": resolved_info.get("workspace_id") or "",
                                "source": resolved_info.get("source") or "",
                                "mode": mode,
                                "operation": "exists",
                                "recovery_actions": [
                                    "choose_existing_file",
                                    "remove_context_file",
                                    "retry",
                                    "exit",
                                ],
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            if not resolved.is_file():
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="context_file_error",
                            message=f"context path is not a file: {path}",
                            details={
                                "path": path,
                                "resolved_path": str(resolved),
                                "display_path": resolved_info.get("display_path") or path,
                                "workspace_id": resolved_info.get("workspace_id") or "",
                                "source": resolved_info.get("source") or "",
                                "mode": mode,
                                "operation": "is_file",
                                "recovery_actions": [
                                    "choose_existing_file",
                                    "remove_context_file",
                                    "retry",
                                    "exit",
                                ],
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
        row = {
            **resolved_info,
            "mode": mode,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "last_modified": body.get("last_modified") or "",
            "size": int(body.get("size") or 0),
            "language": body.get("language") or "",
        }
        bucket = app.state.context_files.setdefault(sid, {})
        bucket[row["path"]] = row
        _flush_context_files(app)
        app.state.bus.publish(
            Event(
                type="context.file.added",
                session_id=sid,
                payload={"session_id": sid, "file": row},
            )
        )
        return row

    @app.delete("/v1/sessions/{sid}/context/files")
    async def remove_context_file(sid: str, request: Request) -> Response:
        """Detach a file by path. 204 whether the path was attached
        — the TUI fires this optimistically on `d` in the context
        pane and doesn't want to error if the file was already
        removed."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        raw_path = (body.get("path") or "").strip()
        path = raw_path[1:].strip() if raw_path.startswith("@") else raw_path
        bucket = app.state.context_files.get(sid, {})
        matched_key = ""
        if path:
            for key, row in bucket.items():
                if path in {
                    key,
                    str(row.get("path") or ""),
                    str(row.get("display_path") or ""),
                    str(row.get("resolved_path") or ""),
                }:
                    matched_key = key
                    break
        if matched_key:
            _guard_direct_destructive_action(
                app,
                session_id=sid,
                workspace_id=sess.workspace_id,
                tool_name="gact.context_file.delete",
                args={"session_id": sid, "path": path},
                summary=f"detach context file {path} from session {sid}",
                reason="user_requested_context_file_delete",
            )
        removed = bucket.pop(matched_key, None) if matched_key else None
        if removed is not None:
            _flush_context_files(app)
            app.state.bus.publish(
                Event(
                    type="context.file.removed",
                    session_id=sid,
                    payload={"session_id": sid, "path": path},
                )
            )
        return Response(status_code=204)

    # ---- POST /v1/sessions/{sid}/fork (BBB26) -------------------------

    @app.post("/v1/sessions/{sid}/fork")
    async def fork_session(sid: str, request: Request) -> Response:
        """Copy a session + its messages into a fresh session.

        Body (optional): ``{"at_message_id": "<id>", "title": "..."}``
        ``at_message_id`` truncates the copy at + including that
        message (so "branch from this point"). Absent → copy every
        stored message.

        The new session's ``parent_session_id`` points at the source
        so the TUI's sidebar can render the fork hierarchy (the v0.1
        Session already carries that field).
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        at = body.get("at_message_id") or ""
        title = body.get("title") or f"{sess.title} (fork)"

        src_msgs = list(app.state.messages.get(sid, []))
        if at:
            kept: list[Message] = []
            for m in src_msgs:
                kept.append(m)
                if m.id == at:
                    break
            src_msgs = kept

        new_sess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=title,
            parent_session_id=sid,
        )
        # Deep-copy parts so the fork's message log doesn't alias the
        # source's. Pydantic's model_copy gives us a snapshot.
        _replace_session_messages(
            app,
            new_sess.id,
            [m.model_copy(deep=True) for m in src_msgs],
        )
        source_context_files = app.state.context_files.get(sid, {})
        if source_context_files:
            app.state.context_files[new_sess.id] = {
                key: dict(row) for key, row in source_context_files.items()
            }
        app.state.sessions.update(new_sess.id, message_count=len(src_msgs))
        return JSONResponse(
            status_code=201,
            content=Session(**new_sess.to_wire()).model_dump(exclude_none=True),
        )

    # ---- /v1/sessions/{sid}/tasks + /v1/tasks/{tid} (#18) ------------

    def _task_id() -> str:
        return f"task_{uuid.uuid4().hex[:12]}"

    @app.get("/v1/sessions/{sid}/tasks")
    async def list_session_tasks(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = list(app.state.session_tasks.get(sid, {}).values())
        return {"tasks": rows}

    @app.post("/v1/sessions/{sid}/tasks")
    async def create_session_task(sid: str, request: Request) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: title",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        status = body.get("status") or "pending"
        if status not in {"pending", "running", "completed", "failed"}:
            status = "pending"
        tid = _task_id()
        row = {
            "id": tid,
            "session_id": sid,
            "title": title,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        app.state.session_tasks.setdefault(sid, {})[tid] = row
        return row

    def _find_task(tid: str) -> Optional[tuple[str, dict[str, Any]]]:
        for sid_key, rows in app.state.session_tasks.items():
            if tid in rows:
                return sid_key, rows[tid]
        return None

    @app.patch("/v1/tasks/{tid}")
    async def patch_task(tid: str, request: Request) -> dict[str, Any]:
        found = _find_task(tid)
        if found is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"task not found: {tid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        _, row = found
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if "title" in body and body["title"]:
            row["title"] = str(body["title"])
        if "status" in body and body["status"] in {"pending", "running", "completed", "failed"}:
            row["status"] = body["status"]
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        return row

    @app.delete("/v1/tasks/{tid}")
    async def delete_task(tid: str) -> Response:
        found = _find_task(tid)
        if found is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"task not found: {tid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        sid_key, _row = found
        sess = app.state.sessions.get(sid_key)
        _guard_direct_destructive_action(
            app,
            session_id=sid_key,
            workspace_id=getattr(sess, "workspace_id", ""),
            tool_name="gact.task.delete",
            args={"task_id": tid, "session_id": sid_key},
            summary=f"delete task {tid}",
            reason="user_requested_task_delete",
        )
        app.state.session_tasks[sid_key].pop(tid, None)
        return Response(status_code=204)

    # ---- /v1/commands + dispatch (#14) --------------------------------

    _BACKEND_COMMANDS: list[dict[str, Any]] = [
        {
            "id": "/clear",
            "title": "Clear session messages",
            "description": "Drop the in-memory log for the active session (does NOT touch ARC).",
            "source": "builtin",
            "status": "available",
            "enabled": True,
            "error": "",
        },
        {
            "id": "/cache-stats",
            "title": "ARC cache stats",
            "description": "Append the current ARC cache hit/miss counters as a system message.",
            "source": "builtin",
            "status": "available",
            "enabled": True,
            "error": "",
        },
        {
            "id": "/dump-trace",
            "title": "Dump last reasoning trace",
            "description": "Append the last assistant turn's DSPy reasoning (when available).",
            "source": "builtin",
            "status": "available",
            "enabled": True,
            "error": "",
        },
        {
            "id": "/optimize",
            "title": "Optimize active expert",
            "description": "Unavailable until optimizer command execution is wired.",
            "source": "builtin",
            "status": "unavailable",
            "enabled": False,
            "error": "not_implemented",
            "disabled_reason": "optimizer command execution is not wired yet",
        },
    ]

    def _normalize_command_id(raw: Any) -> str:
        value = str(raw or "").strip()
        if not value:
            return ""
        return value if value.startswith("/") else f"/{value}"

    def _command_defs_from_agent(agent_def: AgentDef) -> list[dict[str, Any]]:
        metadata = agent_def.metadata if isinstance(agent_def.metadata, Mapping) else {}
        raw_defs: list[Any] = []
        for key in ("commands", "slash_commands", "slash-commands"):
            value = metadata.get(key)
            if isinstance(value, list):
                raw_defs.extend(value)
            elif value:
                raw_defs.append(value)
        for key in ("command", "slash_command", "slash-command"):
            value = metadata.get(key)
            if value:
                raw_defs.append(value)

        rows: list[dict[str, Any]] = []
        for raw in raw_defs:
            if isinstance(raw, str):
                command_id = _normalize_command_id(raw)
                row: dict[str, Any] = {}
            elif isinstance(raw, Mapping):
                command_id = _normalize_command_id(
                    raw.get("id") or raw.get("name") or raw.get("command")
                )
                row = dict(raw)
            else:
                continue
            if not command_id:
                continue
            status = str(row.get("status") or "available")
            enabled = _truthy_command_field(row.get("enabled"), status == "available")
            if status != "available":
                enabled = False
            agent_invocable = _truthy_command_field(
                row.get("agent_invocable", row.get("agent-invocable")),
                False,
            )
            user_invocable = _truthy_command_field(
                row.get("user_invocable", row.get("user-invocable")),
                True,
            )
            rows.append(
                {
                    "id": command_id,
                    "title": str(row.get("title") or agent_def.title or command_id),
                    "description": str(row.get("description") or agent_def.description or ""),
                    "source": "user",
                    "status": status,
                    "enabled": enabled,
                    "error": str(row.get("error") or ""),
                    "disabled_reason": str(row.get("disabled_reason") or ""),
                    "agent_id": agent_def.id,
                    "agent_source": agent_def.source,
                    "invocation": str(row.get("invocation") or "agent"),
                    "user_invocable": user_invocable,
                    "agent_invocable": agent_invocable,
                    "argument_hint": str(
                        row.get("argument_hint") or row.get("argument-hint") or ""
                    ),
                    "arguments": row.get("arguments") or [],
                    "prompt_template": str(
                        row.get("prompt_template")
                        or row.get("prompt-template")
                        or row.get("prompt")
                        or metadata.get("prompt_template")
                        or metadata.get("prompt-template")
                        or ""
                    ),
                }
            )
        return rows

    def _user_command_rows() -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for command in _load_command_files_from_disk():
            rows.setdefault(command["id"], command)
        agents = [AgentDef(**row.to_wire()) for row in app.state.user_agents.list()]
        agents.extend(_load_skills_from_disk())
        for agent_def in agents:
            for command in _command_defs_from_agent(agent_def):
                rows.setdefault(command["id"], command)
        return sorted(rows.values(), key=lambda row: row["id"])

    def _all_command_rows() -> list[dict[str, Any]]:
        rows = {command["id"]: dict(command) for command in _BACKEND_COMMANDS}
        for command in _user_command_rows():
            rows.setdefault(command["id"], command)
        return list(rows.values())

    def _render_command_prompt(
        command_meta: Mapping[str, Any],
        *,
        user_input: str,
        args: Any,
        cmd_id: str,
        agent_id: str,
    ) -> str:
        prompt_template = str(command_meta.get("prompt_template") or "")
        if not prompt_template:
            return user_input or str(command_meta.get("description") or cmd_id)
        rendered = (
            prompt_template.replace("{{input}}", user_input)
            .replace("{{args}}", user_input)
            .replace("$ARGUMENTS", user_input)
            .replace("{{command}}", cmd_id)
            .replace("{{agent_id}}", agent_id)
        )
        if isinstance(args, Mapping):
            for key, value in args.items():
                rendered = rendered.replace(f"{{{{args.{key}}}}}", str(value))
        return rendered

    def _command_required_argument_names(command_meta: Mapping[str, Any]) -> list[str]:
        specs = command_meta.get("arguments") or []
        if isinstance(specs, str):
            return [specs] if specs.strip() else []
        if not isinstance(specs, list):
            return []
        required: list[str] = []
        for spec in specs:
            if isinstance(spec, str) and spec.strip():
                required.append(spec.strip())
            elif isinstance(spec, Mapping) and _truthy_command_field(
                spec.get("required"),
                False,
            ):
                name = str(spec.get("name") or spec.get("id") or "").strip()
                if name:
                    required.append(name)
        return required

    def _validate_command_arguments(
        command_meta: Mapping[str, Any],
        *,
        args: Any,
        user_input: str,
        cmd_id: str,
    ) -> None:
        required = _command_required_argument_names(command_meta)
        if not required:
            return
        if not isinstance(args, Mapping):
            if user_input:
                return
            missing = required
        else:
            missing = [name for name in required if args.get(name) in (None, "")]
        if not missing:
            return
        raise HTTPException(
            status_code=422,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="invalid_arguments",
                    message=f"command {cmd_id} is missing required arguments",
                    details={
                        "command": cmd_id,
                        "missing": missing,
                        "argument_hint": command_meta.get("argument_hint", ""),
                    },
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )

    @app.get("/v1/commands")
    async def list_commands() -> dict[str, Any]:
        """SPEC §6.13 — backend-provided slash commands."""

        return {"commands": _all_command_rows()}

    @app.post("/v1/sessions/{sid}/commands/{cmd}")
    async def dispatch_command(sid: str, cmd: str, request: Request) -> dict[str, Any]:
        """Dispatch a backend command for a session. Returns a
        system-style result the TUI can render inline as a message.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        # Accept "clear" or "/clear"; the TUI sends both shapes.
        cmd_id = cmd if cmd.startswith("/") else "/" + cmd
        commands_by_id = {c["id"]: c for c in _all_command_rows()}
        command_meta = commands_by_id.get(cmd_id)
        if command_meta is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown command: {cmd_id}",
                        details={"known": sorted(commands_by_id)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        if command_meta.get("status") != "available" or command_meta.get("enabled") is False:
            raise HTTPException(
                status_code=501,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error=str(command_meta.get("error") or "unavailable"),
                        message=(
                            f"Backend command {cmd_id} is unavailable: "
                            f"{command_meta.get('disabled_reason') or command_meta.get('error')}"
                        ),
                        details={
                            "command": cmd_id,
                            "status": command_meta.get("status"),
                            "disabled_reason": command_meta.get("disabled_reason", ""),
                            "recovery_actions": [
                                "retry_after_optimizer_support_lands",
                                "exit",
                            ],
                        },
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        try:
            request_body = await request.json()
        except Exception:
            request_body = {}
        if not isinstance(request_body, dict):
            request_body = {}

        if command_meta.get("source") == "user":
            agent_id = str(command_meta.get("agent_id") or "")
            agent_def = _resolve_dynamic_agent(app, agent_id)
            if agent_def is None:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="not_found",
                            message=f"command agent not found: {agent_id}",
                            details={"command": cmd_id, "agent_id": agent_id},
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            args = request_body.get("args")
            if args is None:
                args = request_body.get("arguments")
            user_input = str(
                request_body.get("input")
                or request_body.get("text")
                or request_body.get("prompt")
                or ""
            ).strip()
            if not user_input and args not in (None, ""):
                if isinstance(args, str):
                    user_input = args
                elif isinstance(args, Mapping) and len(args) == 1:
                    user_input = str(next(iter(args.values())))
                else:
                    user_input = json.dumps(args, sort_keys=True, default=str)
            _validate_command_arguments(
                command_meta,
                args=args,
                user_input=user_input,
                cmd_id=cmd_id,
            )
            question = _render_command_prompt(
                command_meta,
                user_input=user_input,
                args=args,
                cmd_id=cmd_id,
                agent_id=agent_id,
            )
            if agent_def.tools:
                pred = _run_tool_user_agent(app.state.agent, agent_def, question, sid)
            else:
                pred = _run_prompt_user_agent(app.state.agent, agent_def, question, sid)
            agent_body_text = str(getattr(pred, "answer", "") or "").strip()
            if not agent_body_text:
                agent_body_text = f"user command {cmd_id} completed with no answer"

            from clio_agent.gact.types import Message, Part, Tokens  # noqa: PLC0415

            sys_msg = Message(
                id=f"msg_cmd_{uuid.uuid4().hex[:10]}",
                session_id=sid,
                role="assistant",
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                parts=[
                    Part(
                        id=f"part_cmd_{uuid.uuid4().hex[:10]}",
                        type="text",
                        metadata={
                            "synthetic": "command_result",
                            "command": cmd_id,
                            "agent_id": agent_id,
                        },
                        text=agent_body_text,
                    )
                ],
                tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
                cost_usd=0.0,
                stop_reason="end_turn",
                metadata={
                    "synthetic": "command_result",
                    "command": cmd_id,
                    "agent_id": agent_id,
                    "route_source": "user_command",
                },
            )
            _append_session_message(app, sid, sys_msg)
            app.state.sessions.update(sid, message_count=len(app.state.messages.get(sid, [])))
            app.state.bus.publish(
                Event(
                    type="message.created",
                    session_id=sid,
                    payload=sys_msg.model_dump(exclude_none=True),
                )
            )
            return {
                "command": cmd_id,
                "session_id": sid,
                "result": {
                    "type": "agent_message",
                    "text": agent_body_text,
                    "agent_id": agent_id,
                },
            }

        # Side effects + system message body per command.
        body_text: str
        if cmd_id == "/clear":
            _guard_direct_destructive_action(
                app,
                session_id=sid,
                workspace_id=sess.workspace_id,
                tool_name="gact.session.clear",
                args={"session_id": sid, "command": cmd_id},
                summary=f"clear session messages for {sid}",
                reason="user_requested_session_clear",
            )
            _delete_session_messages(app, sid)
            app.state.sessions.update(sid, message_count=0)
            app.state.bus.publish(
                Event(
                    type="session.cleared",
                    session_id=sid,
                    payload={"session_id": sid},
                )
            )
            body_text = "session messages cleared"
        elif cmd_id == "/cache-stats":
            stats: dict[str, Any] = {}
            if app.state.arc is not None:
                try:
                    stats = app.state.arc.get_cache_stats() or {}
                except Exception as exc:
                    raise HTTPException(
                        status_code=500,
                        detail=ErrorEnvelope(
                            error=ErrorInfo(
                                error="command_error",
                                message=(
                                    "Backend command /cache-stats could not read ARC "
                                    "cache statistics."
                                ),
                                details={
                                    "command": cmd_id,
                                    "original_error": str(exc),
                                    "recovery_actions": [
                                        "retry",
                                        "reconfigure_provider",
                                        "exit",
                                    ],
                                },
                                recoverable=True,
                            )
                        ).model_dump(exclude_none=True),
                    ) from exc
            body_text = (
                f"ARC cache: hits={stats.get('hits', 0)} "
                f"misses={stats.get('misses', 0)} "
                f"hit_rate={stats.get('hit_rate', 0.0):.2f} "
                f"capacity={stats.get('capacity', 0)}"
            )
        elif cmd_id == "/dump-trace":
            log = app.state.messages.get(sid, [])
            last_asst = next((m for m in reversed(log) if m.role == "assistant"), None)
            if last_asst is None:
                body_text = "no assistant turns yet"
            else:
                trace_part = next(
                    (p for p in last_asst.parts if p.type == "thinking"),
                    None,
                )
                body_text = (
                    trace_part.text
                    if trace_part is not None
                    else "no thinking trace on the last turn"
                )
        else:  # pragma: no cover - guarded above
            body_text = f"unhandled command: {cmd_id}"

        # Materialise body_text as a real assistant message so the TUI
        # actually shows the result. Previously the body_text was only
        # in the POST response — the TUI's runCommandCmd discards that,
        # so /cache-stats, /dump-trace, /optimize, and /clear all looked
        # like they did nothing. Persist + publish so SSE redraws and
        # GET /messages reflects.
        from clio_agent.gact.types import Message, Part, Tokens  # noqa: PLC0415

        sys_msg = Message(
            id=f"msg_cmd_{uuid.uuid4().hex[:10]}",
            session_id=sid,
            role="assistant",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            parts=[
                Part(
                    id=f"part_cmd_{uuid.uuid4().hex[:10]}",
                    type="text",
                    metadata={"synthetic": "command_result", "command": cmd_id},
                    text=f"[{cmd_id}] {body_text}",
                )
            ],
            tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
            cost_usd=0.0,
            stop_reason="end_turn",
            metadata={"synthetic": "command_result", "command": cmd_id},
        )
        _append_session_message(app, sid, sys_msg)
        app.state.bus.publish(
            Event(
                type="message.created",
                session_id=sid,
                payload=sys_msg.model_dump(exclude_none=True),
            )
        )

        return {
            "command": cmd_id,
            "session_id": sid,
            "result": {
                "type": "system_message",
                "text": body_text,
            },
        }

    # ---- /v1/providers (#15) ------------------------------------------

    def _provider_auth_state(preset: "LMProviderPreset") -> tuple[list[str], bool]:
        """Return (auth_methods, is_authenticated) for a preset.

        Maps CLIO's preset flags to the GACT v0.1 §6.12 Provider shape so
        the TUI's settings picker can render the right state badge:

        - argonne_*: globus oauth; authenticated when tokens are on disk
          AND globus-sdk is importable.
        - cloud (requires_api_key=True): api_key auth; authenticated when
          the matching env var is set.
        - local (lm_studio/ollama/codex): no auth required;
          surface as ``["none"]``, always authenticated.
        """
        if preset.provider == "argonne":
            authed = False
            try:
                from clio_agent.providers import argonne_auth  # noqa: PLC0415

                authed = (
                    argonne_auth.tokens_exist()
                    and importlib.util.find_spec("globus_sdk") is not None
                    and argonne_auth.check_auth_status()
                )
            except Exception:
                authed = False
            return ["oauth"], authed

        if preset.requires_api_key:
            env_var = {
                "anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY",
            }.get(preset.provider, "CLIO_LM_API_KEY")
            return ["api_key"], bool(os.environ.get(env_var) or os.environ.get("CLIO_LM_API_KEY"))

        return ["none"], True

    def _provider_to_wire(preset: "LMProviderPreset") -> dict[str, Any]:
        auth_methods, is_authed = _provider_auth_state(preset)
        return {
            "id": preset.id,
            "name": preset.label,
            "auth_methods": auth_methods,
            "is_authenticated": is_authed,
            "default_model": preset.suggested_model,
            "api_base": preset.api_base,
            "env_keys": (["CLIO_LM_API_KEY"] if preset.requires_api_key else []),
            "description": preset.description,
            "metadata": {
                "provider_kind": preset.provider,
                "requires_api_key": preset.requires_api_key,
            },
        }

    @app.get("/v1/providers")
    async def list_providers() -> dict[str, Any]:
        """SPEC §6.12 — generic LM provider catalog.

        Returns one row per preset with the v0.1 fields (id, name,
        auth_methods, is_authenticated, default_model) so the TUI's
        settings picker can render the right state badge per provider
        and decide whether to surface a "Login" affordance.
        """

        return {"providers": [_provider_to_wire(p) for p in _LM_PRESETS]}

    # GET /v1/providers/{provider_id} is registered after the literal
    # /v1/providers/lm route so the LM configuration endpoint keeps
    # winning FastAPI's order-based route match.

    @app.post("/v1/providers/{provider_id}/auth")
    async def auth_provider(provider_id: str, request: Request) -> dict[str, Any]:
        """SPEC §6.12 — kick off provider-specific auth.

        For argonne_*, this launches the Globus OAuth flow in an
        interactive terminal where the user can visit the URL and
        paste the generated code. This endpoint must not validate or
        refresh cached tokens inline: expired Globus sessions can
        block waiting for terminal input, which would freeze the TUI
        request instead of giving the user an actionable login path.

        Other providers (cloud / local) use api_key / no-auth and
        return 405 with a hint pointing to PUT /v1/providers/lm.
        """

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        if preset.provider != "argonne":
            raise HTTPException(
                status_code=405,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="unsupported",
                        message=(
                            f"provider '{provider_id}' uses "
                            f"{'api_key' if preset.requires_api_key else 'no'} "
                            "auth; pass api_key directly to PUT /v1/providers/lm."
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        if importlib.util.find_spec("globus_sdk") is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_missing",
                        message=(
                            "globus-sdk not installed. Install with "
                            "'pip install clio-agent[argonne]' on the "
                            "backend host and retry."
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        force = bool(body.get("force", False))

        command = [
            sys.executable,
            "-m",
            "clio_agent.providers.argonne_auth",
            "authenticate",
        ]
        if force:
            command.append("--force")
        manual_command = " ".join(command)
        try:
            if os.name == "nt":
                powershell = (
                    shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
                )
                command_literal = " ".join(
                    f"'{part.replace(chr(39), chr(39) + chr(39))}'" for part in command
                )
                ps_script = (
                    "$Host.UI.RawUI.WindowTitle = 'CLIO ALCF Globus Login'; "
                    "Write-Host 'CLIO ALCF Globus login'; "
                    f"Write-Host 'Running: {manual_command.replace(chr(39), chr(39) + chr(39))}'; "
                    "Write-Host ''; "
                    f"& {command_literal}; "
                    "$exitCode = $LASTEXITCODE; "
                    "Write-Host ''; "
                    "Write-Host ('Auth helper exited with code ' + $exitCode); "
                    "Read-Host 'Press Enter to close this window'"
                )
                subprocess.Popen(  # noqa: S603
                    [
                        powershell,
                        "-NoExit",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        ps_script,
                    ],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
                instructions = (
                    "Opened a persistent PowerShell window for ALCF Globus login. Complete the "
                    "authorization code flow there, then press Ctrl+R here to refresh provider status. "
                    f"If no terminal appears, run: {manual_command}"
                )
            else:
                terminal = next(
                    (
                        shutil.which(name)
                        for name in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")
                        if shutil.which(name)
                    ),
                    None,
                )
                if terminal:
                    term_name = os.path.basename(terminal)
                    args = (
                        [terminal, "--", *command]
                        if term_name == "gnome-terminal"
                        else [terminal, "-e", *command]
                    )
                    subprocess.Popen(args)  # noqa: S603
                    instructions = (
                        "Opened a terminal for ALCF Globus login. Complete the "
                        "authorization code flow there, then press Ctrl+R here to refresh provider status. "
                        f"If no terminal appears, run: {manual_command}"
                    )
                else:
                    instructions = (
                        "Run this in an interactive terminal, then press Ctrl+R here: "
                        + manual_command
                    )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="argonne_auth_failed",
                        message=f"Could not launch interactive Globus authentication: {exc}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        return {
            "is_authenticated": False,
            "provider_id": provider_id,
            "instructions": instructions,
        }

    # Per-provider model catalogs. Hand-curated rather than introspected
    # because most upstreams either don't expose a /models endpoint or
    # return hundreds of irrelevant entries. The TUI's Settings → Model
    # picker calls this once per provider and lists the rows verbatim.
    # Derived from clio_agent.providers.registry. Static fallback used
    # only when live model discovery against the upstream /v1/models
    # endpoint fails (no key, network down, 5xx) — see the GET
    # /v1/providers/{id}/models handler below for the resolution order.
    # ALCF / Argonne live model availability is dynamic (jobs spin up
    # and tear down behind the gateway); the live set can be queried
    # with `scripts/list_active_models.sh` in alcf-agentics-workflow.
    from clio_agent.providers.registry import (
        as_provider_models_dict as _build_provider_models,
    )

    _PROVIDER_MODELS: dict[str, list[dict[str, str]]] = _build_provider_models()

    # Cache for live model discovery. Keyed by preset id (or
    # "argonne:<cluster>" for the cluster-aware argonne path); value
    # is (epoch_seconds, [models]). 30 s TTL keeps the picker snappy
    # if the user spams ←/→ but doesn't mask backend churn (ALCF
    # rotates loaded models as PBS jobs come and go; LM Studio swaps
    # models on user action).
    _LIVE_MODELS_TTL_S = 30.0
    _ARGONNE_JOBS_TIMEOUT_S = 12.0
    # Cache value: (epoch_seconds, models, source, error_message). Source
    # is "live" / "static_catalog" / "unavailable"; error_message is the human-readable
    # reason live failed (empty when source=="live"). Surfacing this on
    # /v1/providers/{id}/models lets the TUI render a banner instead of
    # silently lying with a stale catalog.
    _live_models_cache: dict[str, tuple[float, list[dict[str, str]], str, str]] = {}

    def _argonne_live_models(
        cluster: str,
        chat_base: str = "",
    ) -> tuple[list[dict[str, str]], str, str]:
        """Hit the ALCF endpoint catalog and return ``(models, source,
        error_message)`` for the provider picker.

        ``/resource_server/list-endpoints`` is the documented model
        catalog. ``/{cluster}/jobs`` is used only to annotate models
        that are currently live or queued.
        """
        cache_key = f"argonne:{cluster}"
        now = time.time()
        cached = _live_models_cache.get(cache_key)
        if cached is not None and now - cached[0] < _LIVE_MODELS_TTL_S:
            return cached[1], cached[2], cached[3]

        def _fallback(reason: str) -> tuple[list[dict[str, str]], str, str]:
            empty: list[dict[str, str]] = []
            _live_models_cache[cache_key] = (now, empty, "unavailable", reason)
            return empty, "unavailable", reason

        # Accept CLIO's own override OR the env var alcf-agentics-
        # workflow uses (ALCF_INFERENCE_TOKEN / access_token).
        token = (
            os.environ.get("CLIO_ARGONNE_TOKEN", "").strip()
            or os.environ.get("ALCF_INFERENCE_TOKEN", "").strip()
            or os.environ.get("access_token", "").strip()
        )
        token_source = "env"
        if not token:
            try:
                from clio_agent.providers.argonne_auth import (  # noqa: PLC0415
                    get_access_token,
                    tokens_exist,
                )

                if tokens_exist():
                    token = get_access_token()
                    token_source = "globus_disk"
            except Exception as exc:
                return _fallback(
                    "no token available — globus refresh failed: "
                    f"{exc}. Re-auth: `python -m clio_agent.providers"
                    ".argonne_auth authenticate -f`"
                )
        if not token:
            return _fallback(
                "no token available. Set CLIO_ARGONNE_TOKEN / "
                "ALCF_INFERENCE_TOKEN, or run `python -m clio_agent."
                "providers.argonne_auth authenticate -f` once to "
                "store one in ~/.globus."
            )

        framework = "api" if "/api/" in chat_base else "vllm"
        try:
            import requests  # noqa: PLC0415

            catalog_response = requests.get(
                "https://inference-api.alcf.anl.gov/resource_server/list-endpoints",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_ARGONNE_JOBS_TIMEOUT_S,
            )
        except Exception as exc:
            return _fallback(f"ALCF gateway unreachable: {exc}. Check network / proxy.")

        if catalog_response.status_code == 401:
            return _fallback(
                f"ALCF token rejected (401, source={token_source}). "
                "Token likely expired - re-auth: `python -m "
                "clio_agent.providers.argonne_auth authenticate -f` "
                "and re-export ALCF_INFERENCE_TOKEN before redeploying."
            )
        if catalog_response.status_code >= 400:
            return _fallback(
                "ALCF endpoint catalog returned HTTP "
                f"{catalog_response.status_code}: {(catalog_response.text or '')[:200]}"
            )

        try:
            catalog_payload = catalog_response.json()
        except Exception as exc:
            return _fallback(f"ALCF endpoint catalog response not JSON: {exc}")

        framework_payload = (
            (catalog_payload.get("clusters") or {})
            .get(cluster, {})
            .get("frameworks", {})
            .get(framework, {})
        )
        catalog_models = [
            str(model).strip()
            for model in framework_payload.get("models") or []
            if str(model).strip()
        ]
        if not catalog_models:
            return _fallback(f"ALCF endpoint catalog has no {cluster}/{framework} models")

        running_details: dict[str, str] = {}
        try:
            jobs_response = requests.get(
                f"https://inference-api.alcf.anl.gov/resource_server/{cluster}/jobs",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_ARGONNE_JOBS_TIMEOUT_S,
            )
            jobs_payload = jobs_response.json() if jobs_response.status_code < 400 else {}
        except Exception:
            jobs_payload = {}

        for job in jobs_payload.get("running") or []:
            for raw in (job.get("Models") or "").split(","):
                mid = raw.strip()
                if not mid or mid in running_details:
                    continue
                walltime = (job.get("Walltime") or "").strip()
                nodes = (job.get("Nodes Reserved") or "").strip()
                desc = f"live on {cluster}"
                if nodes:
                    desc += f" ({nodes} node{'s' if nodes != '1' else ''})"
                if walltime:
                    desc += f", walltime {walltime}"
                running_details[mid] = desc

        seen: set[str] = set()
        models: list[dict[str, str]] = []
        for mid in catalog_models:
            if mid in seen:
                continue
            seen.add(mid)
            name = mid.split("/", 1)[-1] if "/" in mid else mid
            desc = running_details.get(mid, f"available on {cluster}/{framework}")
            models.append({"id": mid, "name": name, "description": desc})

        _live_models_cache[cache_key] = (now, models, "live", "")
        return models, "live", ""

        try:
            import requests  # noqa: PLC0415

            r = requests.get(
                f"https://inference-api.alcf.anl.gov/resource_server/{cluster}/jobs",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_ARGONNE_JOBS_TIMEOUT_S,
            )
        except Exception as exc:
            return _fallback(f"ALCF gateway unreachable: {exc}. Check network / proxy.")

        if r.status_code == 401:
            return _fallback(
                f"ALCF token rejected (401, source={token_source}). "
                "Token likely expired — re-auth: `python -m "
                "clio_agent.providers.argonne_auth authenticate -f` "
                "and re-export ALCF_INFERENCE_TOKEN before redeploying."
            )
        if r.status_code >= 400:
            return _fallback(f"ALCF gateway returned HTTP {r.status_code}: {(r.text or '')[:200]}")

        try:
            payload = r.json()
        except Exception as exc:
            return _fallback(f"ALCF response not JSON: {exc}")

        jobs_seen: set[str] = set()
        jobs_models: list[dict[str, str]] = []
        for job in payload.get("running") or []:
            for raw in (job.get("Models") or "").split(","):
                mid = raw.strip()
                if not mid or mid in jobs_seen:
                    continue
                jobs_seen.add(mid)
                name = mid.split("/", 1)[-1] if "/" in mid else mid
                walltime = (job.get("Walltime") or "").strip()
                nodes = (job.get("Nodes Reserved") or "").strip()
                desc = f"loaded on {cluster}"
                if nodes:
                    desc += f" ({nodes} node{'s' if nodes != '1' else ''})"
                if walltime:
                    desc += f", walltime {walltime}"
                jobs_models.append({"id": mid, "name": name, "description": desc})

        if not jobs_models:
            # /jobs returned 0 running — could be "cluster idle (PBS
            # jobs cycle)" OR "cluster in maintenance". The maintenance
            # signal lives behind /chat/completions, not /jobs:
            #
            #   "Error: Sophia cluster currently unavailable due to
            #    maintenance. Expected to come back online around 3pm
            #    Central."
            #
            # Probe that endpoint with a 1-token payload to discover
            # the gateway's actual status message and surface it
            # verbatim, instead of guessing at "idle". 2-second budget
            # so a hung gateway doesn't stall the picker.
            queued = len(payload.get("queued") or [])
            stopped = len(payload.get("stopped") or [])
            empty: list[dict[str, str]] = []
            maintenance_msg = ""
            # Sophia hangs the framework path off /vllm/v1; Metis off
            # /api/v1; future clusters could differ again. Use the
            # preset's api_base when supplied (it already encodes the
            # right framework path); fall back to the sophia layout
            # for the bare-kind call site.
            probe_base = (
                chat_base.rstrip("/")
                if chat_base
                else (f"https://inference-api.alcf.anl.gov/resource_server/{cluster}/vllm/v1")
            )
            try:
                probe_model = "gpt-oss-120b" if cluster == "metis" else "openai/gpt-oss-120b"
                probe = requests.post(
                    f"{probe_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": probe_model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "temperature": 0,
                    },
                    timeout=2,
                )
                # Gateway returns maintenance text as a JSON-encoded
                # bare string body. Tolerate either bare string or
                # {"detail": "..."} envelope.
                try:
                    body = probe.json()
                except Exception:
                    body = probe.text
                text = (
                    body
                    if isinstance(body, str)
                    else (body.get("detail") if isinstance(body, dict) else "") or ""
                )
                if isinstance(text, str) and text.lower().startswith("error:"):
                    maintenance_msg = text
            except Exception:
                pass

            if maintenance_msg:
                msg = f"ALCF {cluster}: {maintenance_msg}"
            else:
                details = []
                if queued:
                    details.append(f"{queued} queued")
                if stopped:
                    details.append(f"{stopped} recently stopped")
                tail = f" ({', '.join(details)})" if details else ""
                msg = (
                    f"ALCF {cluster} has no models loaded right now"
                    f"{tail}. PBS jobs cycle — check back in a few minutes, "
                    f"or visit https://docs.alcf.anl.gov/services/inference-endpoints/ "
                    f"for current status."
                )
            _live_models_cache[cache_key] = (now, empty, "unavailable", msg)
            return empty, "unavailable", msg

        _live_models_cache[cache_key] = (now, jobs_models, "live", "")
        return jobs_models, "live", ""

    def _openai_compat_live_models(
        preset: "LMProviderPreset",
        *,
        api_base_override: str = "",
    ) -> tuple[list[dict[str, Any]], str, str]:
        """Discover models for any OpenAI-compatible preset.

        Returns ``(models, source, error_message)`` so the TUI can
        render an actionable warning when live discovery fell back.
        """
        base = (api_base_override or preset.api_base or "").rstrip("/")
        cache_key = f"preset:{preset.id}:{base}"
        now = time.time()
        cached = _live_models_cache.get(cache_key)
        if cached is not None and now - cached[0] < _LIVE_MODELS_TTL_S:
            return cached[1], cached[2], cached[3]

        static = list(_PROVIDER_MODELS.get(preset.id) or _PROVIDER_MODELS.get(preset.provider, []))

        def _fallback(reason: str) -> tuple[list[dict[str, Any]], str, str]:
            empty: list[dict[str, Any]] = []
            _live_models_cache[cache_key] = (now, empty, "unavailable", reason)
            return empty, "unavailable", reason

        if not preset.supports_live_catalog:
            _live_models_cache[cache_key] = (now, static, "static_catalog", "")
            return static, "static_catalog", ""
        if not base:
            return _fallback("preset has no api_base — nothing to query")
        url = base + "/models"

        if preset.provider == "lm_studio":
            try:
                from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

                import requests  # noqa: PLC0415

                parts = urlsplit(base)
                root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
                native = requests.get(f"{root}/api/v1/models", timeout=4)
                if native.status_code < 400:
                    payload = native.json()
                    raw_models = payload.get("models") if isinstance(payload, dict) else None
                    if isinstance(raw_models, list):
                        seen_native: set[str] = set()
                        native_models: list[dict[str, Any]] = []
                        for item in raw_models:
                            if not isinstance(item, dict) or item.get("type") == "embedding":
                                continue
                            mid = (item.get("key") or item.get("id") or "").strip()
                            if not mid or mid in seen_native:
                                continue
                            seen_native.add(mid)
                            details = [f"live from {preset.label}"]
                            params = (item.get("params_string") or "").strip()
                            quant = item.get("quantization")
                            if isinstance(quant, dict) and quant.get("name"):
                                details.append(f"{quant['name']}")
                            if params:
                                details.append(params)
                            native_models.append(
                                {
                                    "id": mid,
                                    "name": (item.get("display_name") or mid).strip(),
                                    "description": " · ".join(details),
                                    "context_window": int(item.get("max_context_length") or 0),
                                }
                            )
                        if native_models:
                            _live_models_cache[cache_key] = (now, native_models, "live", "")
                            return native_models, "live", ""
            except Exception:
                # Older LM Studio builds may not expose /api/v1/models;
                # fall through to the OpenAI-compatible /v1/models path.
                pass

        headers: dict[str, str] = {}
        if preset.provider == "anthropic":
            env_key = _preset_api_key_env(preset)
            key = os.environ.get(env_key) or os.environ.get("CLIO_LM_API_KEY") or ""
            if not key:
                return _fallback(f"missing {env_key}")
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            env_key = _preset_api_key_env(preset)
            key = (
                os.environ.get(env_key)
                or os.environ.get("CLIO_LM_API_KEY")
                or {"lm_studio": "lm-studio", "ollama": "ollama"}.get(preset.provider, "")
            )
            if preset.requires_api_key and not key:
                return _fallback(f"missing {env_key}")
            if key:
                headers["Authorization"] = f"Bearer {key}"

        try:
            import requests  # noqa: PLC0415

            r = requests.get(url, headers=headers, timeout=4)
        except Exception as exc:
            return _fallback(f"{preset.label} unreachable: {exc}")

        if r.status_code == 401:
            return _fallback(
                f"{preset.label} rejected the API key (401). Check the env var on the backend host."
            )
        if r.status_code >= 400:
            return _fallback(
                f"{preset.label} returned HTTP {r.status_code}: {(r.text or '')[:200]}"
            )

        try:
            payload = r.json()
        except Exception as exc:
            return _fallback(f"{preset.label} response not JSON: {exc}")

        raw = payload.get("data") if isinstance(payload, dict) else payload
        if preset.provider == "ollama" and not isinstance(raw, list):
            try:
                from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

                parts = urlsplit(base)
                root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
                tags = requests.get(f"{root}/api/tags", timeout=4)
                if tags.status_code >= 400:
                    return _fallback(
                        f"{preset.label} is reachable, but /api/tags returned "
                        f"HTTP {tags.status_code}: {(tags.text or '')[:200]}"
                    )
                tags_payload = tags.json()
                raw_tags = tags_payload.get("models") if isinstance(tags_payload, dict) else None
                if isinstance(raw_tags, list):
                    raw = []
                    for item in raw_tags:
                        if not isinstance(item, dict):
                            continue
                        mid = (item.get("model") or item.get("name") or "").strip()
                        if mid:
                            raw.append({"id": mid})
                    if not raw:
                        _live_models_cache[cache_key] = (now, [], "live", "")
                        return [], "live", ""
            except Exception as exc:
                return _fallback(f"{preset.label} /api/tags probe failed: {exc}")
        if not isinstance(raw, list):
            return _fallback(f"{preset.label} response missing data[] array")

        seen: set[str] = set()
        models: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            mid = (item.get("id") or item.get("name") or "").strip()
            if not mid or mid in seen:
                continue
            if "embedding" in mid.lower() or "embed" in mid.lower():
                continue
            seen.add(mid)
            name = mid.split("/", 1)[-1] if "/" in mid else mid
            owner = item.get("owned_by") or ""
            desc = f"live from {preset.label}"
            if owner and owner.lower() not in {"system", "openai-internal"}:
                desc += f" (owned_by {owner})"
            models.append({"id": mid, "name": name, "description": desc})

        if not models and preset.provider == "ollama":
            _live_models_cache[cache_key] = (now, [], "live", "")
            return [], "live", ""
        if not models:
            return _fallback(f"{preset.label} returned an empty model list")
        _live_models_cache[cache_key] = (now, models, "live", "")
        return models, "live", ""

    @app.get("/v1/providers/{provider_id}/models")
    async def list_provider_models(provider_id: str, api_base: str = "") -> dict[str, Any]:
        """Per-provider model catalog — live where possible.

        Resolution:
        - Path is a preset id (``argonne_sophia``, ``anthropic``,
          ``lm_studio``, …): look the preset up. Argonne presets hit
          ALCF's /jobs endpoint (the vLLM /models proxy 405s on the
          gateway). Everyone else uses the OpenAI-compatible
          ``GET {api_base}/models`` discovery (Anthropic, OpenAI,
          OpenRouter, LM Studio, Ollama, vLLM-direct all implement
          that shape).
        - Path is a bare provider kind (``argonne``, ``openai``):
          live-fetch using the kind's first registered preset's
          api_base + auth.
        - Fall through to the static catalog for known provider ids
          that do not have a live-discovery path.

        Live fetches are cached for _LIVE_MODELS_TTL_S so spamming
        ←/→ in the picker doesn't hammer the upstream. Failures
        (no key, network down, 5xx) return the static catalog with
        source="unavailable" and an error message so the picker is
        honest about why saving is disabled. Unknown provider ids
        return a structured 404 instead of pretending to be an empty
        static catalog.
        """

        # Match a preset id first.
        def _wrap(triple: tuple[list[dict[str, str]], str, str]) -> dict[str, Any]:
            models, source, err = triple
            out: dict[str, Any] = {"models": models, "source": source}
            if err:
                out["error"] = err
            return out

        for p in _LM_PRESETS:
            if p.id == provider_id:
                if p.provider == "argonne":
                    cluster = _argonne_cluster_from_preset(p)
                    return _wrap(_argonne_live_models(cluster, p.api_base))
                return _wrap(_openai_compat_live_models(p, api_base_override=api_base))
        # Bare provider kind — pick the first preset that uses this
        # kind so we have an api_base + label to drive discovery.
        if provider_id == "argonne":
            for p in _LM_PRESETS:
                if p.provider == "argonne":
                    cluster = _argonne_cluster_from_preset(p)
                    return _wrap(_argonne_live_models(cluster, p.api_base))
            return _wrap(_argonne_live_models("sophia"))
        for p in _LM_PRESETS:
            if p.provider == provider_id:
                return _wrap(_openai_compat_live_models(p, api_base_override=api_base))
        # Last-ditch static for known provider ids only.
        models = _PROVIDER_MODELS.get(provider_id)
        if models is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        details={"available": sorted(_PROVIDER_MODELS)},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"models": models, "source": "static_catalog"}

    def _argonne_cluster_from_preset(preset: "LMProviderPreset") -> str:
        """Pull the cluster slug ("sophia"/"polaris") out of an
        argonne preset's api_base. Argonne presets all point at
        ``…/resource_server/<cluster>/vllm/v1`` so the slug is the
        path component immediately after ``resource_server``."""
        base = (preset.api_base or "").rstrip("/")
        marker = "/resource_server/"
        idx = base.find(marker)
        if idx == -1:
            return "sophia"
        tail = base[idx + len(marker) :]
        slug = tail.split("/", 1)[0]
        return slug or "sophia"

    # ---- /v1/mcp/servers (#13) ---------------------------------------

    @app.get("/v1/mcp/servers")
    async def list_mcp_servers() -> dict[str, Any]:
        """SPEC §6.7 — enumerate MCP servers the backend has mounted.

        Returns BOTH the bundled in-process servers (fs/hdf5/parquet)
        AND any third-party servers installed via POST /v1/mcp/servers.
        Each row carries id/name/status/transport/tools_count/tools.
        """

        rows = _mcp_server_rows()
        return {"servers": rows}

    def _mcp_server_rows() -> list[dict[str, Any]]:
        """Return bundled plus installed MCP server catalog rows."""
        rows: list[dict[str, Any]] = []
        # In-process bundled servers (fs/hdf5/parquet via gateway).
        try:
            from clio_agent.tools.gateway import list_capabilities

            caps = list_capabilities()
            per_server: dict[str, list[dict[str, str]]] = {}
            for tool in caps:
                srv = tool.get("server", "unknown")
                per_server.setdefault(srv, []).append(tool)
            for name, tools in sorted(per_server.items()):
                rows.append(
                    {
                        "id": f"mcp_{name}",
                        "name": name,
                        "status": "ready",
                        "transport": "in_process",
                        "tools_count": len(tools),
                        "tools": [t["name"] for t in tools],
                    }
                )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "id": "mcp_bundled_error",
                    "name": "bundled-gateway",
                    "status": "error",
                    "transport": "in_process",
                    "tools_count": 0,
                    "tools": [],
                    "error": f"gateway introspection failed: {exc!r}",
                }
            )

        # Third-party servers installed at runtime.
        installed = getattr(app.state, "external_mcp_servers", {})
        for sid, info in sorted(installed.items()):
            rows.append(
                {
                    "id": sid,
                    "name": info.get("name", sid),
                    "status": info.get("status", "unknown"),
                    "transport": info.get("transport", "unknown"),
                    "tools_count": len(info.get("tools") or []),
                    "tools": list(info.get("tools") or []),
                    "spec": info.get("spec", {}),
                }
            )
        return rows

    @app.post("/v1/mcp/servers", status_code=201)
    async def install_mcp_server(request: Request) -> dict[str, Any]:
        """Install + connect to a third-party MCP server.

        Body shapes:
        - stdio:  {"name": "everything", "transport": "stdio",
                   "command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"],
                   "env": {...}}
        - http:   {"name": "remote", "transport": "http",
                   "url": "https://mcp.example.com"}

        Connects via fastmcp.Client, lists the server's tools, and
        records the server in ``app.state.external_mcp_servers`` so
        subsequent /v1/mcp/servers GETs and tool dispatch can see it.

        Returns the same row shape /v1/mcp/servers does.
        """

        try:
            body = await request.json()
        except Exception:
            body = {}
        name = body.get("name") or body.get("id") or "unnamed"
        transport_kind = (body.get("transport") or "stdio").lower()

        try:
            from fastmcp import Client
            from fastmcp.client.transports import (
                StdioTransport,
                StreamableHttpTransport,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_missing",
                        message=f"fastmcp Client unavailable: {exc!r}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        if transport_kind == "stdio":
            command = body.get("command")
            args = body.get("args") or []
            env = body.get("env") or {}
            if not command:
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="bad_request",
                            message="stdio transport requires 'command'",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            transport = StdioTransport(command=command, args=list(args), env=dict(env) or None)
            spec = {"transport": "stdio", "command": command, "args": list(args)}
        elif transport_kind in {"http", "streamable-http"}:
            url = body.get("url")
            if not url:
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="bad_request",
                            message="http transport requires 'url'",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            transport = StreamableHttpTransport(url=url)  # type: ignore[assignment]
            spec = {"transport": "http", "url": url}
        else:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=f"unknown transport: {transport_kind!r} (use stdio|http)",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Probe the server: connect, list tools, disconnect cleanly.
        # We re-create the Client per dispatch later (cheap for stdio,
        # no shared global state to worry about).
        tool_names: list[str] = []
        connect_error: Optional[str] = None
        try:
            async with Client(transport) as client:
                tools = await client.list_tools()
                tool_names = [t.name for t in tools]
        except Exception as exc:  # noqa: BLE001
            connect_error = repr(exc)

        sid = f"mcp_ext_{uuid.uuid4().hex[:10]}"
        if not hasattr(app.state, "external_mcp_servers"):
            app.state.external_mcp_servers = {}
        info = {
            "id": sid,
            "name": name,
            "status": "ready" if connect_error is None else "error",
            "transport": transport_kind,
            "tools": tool_names,
            "spec": spec,
        }
        if connect_error:
            info["error"] = connect_error
        app.state.external_mcp_servers[sid] = info

        if connect_error is not None:
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="upstream_unavailable",
                        message=f"MCP server probe failed: {connect_error}",
                        details={"id": sid, "spec": spec},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return {
            "id": sid,
            "name": name,
            "status": "ready",
            "transport": transport_kind,
            "tools_count": len(tool_names),
            "tools": tool_names,
            "spec": spec,
        }

    @app.post("/v1/mcp/servers/{sid}/call")
    async def call_external_mcp_tool(sid: str, request: Request) -> dict[str, Any]:
        """Invoke a tool on an installed third-party MCP server.

        Body: {"tool": "<tool_name>", "args": {...}}

        Connects via fastmcp.Client using the spec recorded at
        install time, calls the tool, fires the same global
        tool_observer the agent uses (so SSE events + tools_called
        ledger entries land identically to in-process tools), and
        returns the structured result.
        """

        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        info = installed.get(sid)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"no installed MCP server: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        tool_name = body.get("tool")
        tool_args = body.get("args") or {}
        requested_session_id = str(body.get("session_id") or "").strip()
        if not tool_name:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="missing 'tool' in request body",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if requested_session_id and app.state.sessions.get(requested_session_id) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {requested_session_id}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        observer_name = f"{info.get('name', 'ext')}.{tool_name}"
        tool_session = (
            _tool_session_context(requested_session_id) if requested_session_id else nullcontext()
        )
        with tool_session:
            gate = getattr(app.state, "pending_permission_gate", None) or _make_permission_gate(app)
            tool_context = contextvars.copy_context()
            try:
                decision = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: tool_context.run(gate, observer_name, tool_args),
                )
            except PermissionError as exc:
                raise HTTPException(
                    status_code=403,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="permission_error",
                            message=str(exc),
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc
            if decision != "allow":
                raise HTTPException(
                    status_code=403,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="permission_error",
                            message=f"tool call {observer_name!r} denied by permission gate",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )

            try:
                from fastmcp import Client
                from fastmcp.client.transports import (
                    StdioTransport,
                    StreamableHttpTransport,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="dependency_missing",
                            message=f"fastmcp Client unavailable: {exc!r}",
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc

            spec = info.get("spec", {})
            if spec.get("transport") == "stdio":
                transport = StdioTransport(
                    command=spec["command"],
                    args=spec.get("args") or [],
                )
            elif spec.get("transport") == "http":
                transport = StreamableHttpTransport(url=spec["url"])  # type: ignore[assignment]
            else:
                raise HTTPException(
                    status_code=500,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="internal_error",
                            message=f"unknown stored transport: {spec!r}",
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )

            # Fire tool observer manually so this call shows up in
            # tools_called + tool.call.* SSE events identically to an
            # agent-driven tool call. Same observer, no special path.
            tool_observer = getattr(app.state, "pending_tool_observer", None)
            if tool_observer is None:
                tool_observer = _make_tool_observer(app)
            if tool_observer is not None:
                try:
                    tool_observer(observer_name, tool_args, "started", None)
                except Exception:
                    pass
            try:
                async with Client(transport) as client:
                    result = await client.call_tool(tool_name, tool_args)
                content = []
                for c in getattr(result, "content", None) or []:
                    content.append(
                        {
                            "type": getattr(c, "type", "text"),
                            "text": getattr(c, "text", str(c)),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                if tool_observer is not None:
                    try:
                        tool_observer(observer_name, tool_args, "completed", repr(exc))
                    except Exception:
                        pass
                raise HTTPException(
                    status_code=502,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="upstream_error",
                            message=f"tool call failed: {exc!r}",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc
            if tool_observer is not None:
                try:
                    tool_observer(observer_name, tool_args, "completed", None)
                except Exception:
                    pass
            return {
                "server_id": sid,
                "tool": tool_name,
                "args": tool_args,
                "content": content,
                "is_error": getattr(result, "isError", False),
                **({"session_id": requested_session_id} if requested_session_id else {}),
            }

    # ---- /v1/sessions/{sid}/compact (Codex/CC parity) -----------------
    # Summarise the in-memory conversation transcript and replace it with
    # a compact synopsis to reclaim context. The TUI's /compact slash
    # command POSTs here. Today this is opportunistic: we ask the chat
    # agent to produce a one-paragraph summary and store it as a new
    # synthetic system message; the original transcript is preserved for
    # any future /resume work.

    @app.post("/v1/sessions/{sid}/compact")
    async def compact_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        ledger = app.state.messages.get(sid, [])
        if not ledger:
            return {
                "session_id": sid,
                "compacted": False,
                "reason": "session has no messages to compact",
            }

        # Build a transcript blob. Keep enough per-part evidence for scientific
        # identifiers and metrics to survive compaction, while still bounding
        # pathological tool output.
        # ledger entries are Pydantic Message models (see types.py); use
        # attribute access + model_dump() defensively for dict-shaped
        # entries the older code paths still produce.
        def _attr(o, name, default=None):
            if hasattr(o, name):
                return getattr(o, name)
            if isinstance(o, dict):
                return o.get(name, default)
            return default

        per_part_limit = 6000
        transcript_limit = 60000
        chunks: list[str] = []
        transcript_chars = 0
        for m in ledger[-50:]:  # last 50 messages should be enough context
            role = (_attr(m, "role", "user") or "user").upper()
            for p in _attr(m, "parts", []) or []:
                txt = _attr(p, "text", "") or ""
                if len(txt) > per_part_limit:
                    head_limit = per_part_limit // 2
                    tail_limit = per_part_limit - head_limit
                    txt = (
                        txt[:head_limit]
                        + "\n[...part truncated for compaction...]\n"
                        + txt[-tail_limit:]
                    )
                txt = txt.strip()
                if not txt:
                    continue
                chunk = f"{role}: {txt}"
                remaining = transcript_limit - transcript_chars
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining] + "\n[...transcript truncated for compaction...]"
                chunks.append(chunk)
                transcript_chars += len(chunk)
            if transcript_chars >= transcript_limit:
                break
        transcript = "\n".join(chunks)
        if not transcript.strip():
            return {
                "session_id": sid,
                "compacted": False,
                "reason": "transcript is empty after part filtering",
            }

        agent = app.state.agent
        if agent is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="agent_unavailable",
                        message="no LM agent wired; configure one via PUT /v1/providers/lm",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Try to extract optional focus instructions from the body.
        try:
            body = await request.json()
        except Exception:
            body = {}
        focus = (body.get("focus") or "").strip()

        prompt = (
            "Create an evidence-preserving compact memory for the following CLIO "
            "conversation transcript. This memory will replace the archived transcript, "
            "so preserve concrete scientific evidence, not just a high-level story.\n\n"
            "Rules:\n"
            "- Keep exact file paths, dataset names, column names, variable names, "
            "units, dimensions, counts, statistics, artifact paths, and error messages "
            "when they appear in the transcript.\n"
            "- Preserve which findings came from which source, grouped by file/provider "
            "or workflow stage.\n"
            "- Preserve unresolved gaps, failed inspections, missing dependencies, and "
            "next checks.\n"
            "- If evidence is missing or a source was not inspected, say that explicitly. "
            "Do not fill gaps with plausible details.\n"
            "- Do not invent dataset names, columns, statistics, compression settings, "
            "or readiness conclusions that are not supported by the transcript.\n"
            "- Prefer concise structured bullets over prose. Keep the summary compact, "
            "but do not omit identifiers needed for a later expert to continue the work."
        )
        if focus:
            prompt += f"\n\nFocus the summary on: {focus}"
        prompt += f"\n\n--- transcript ---\n{transcript}\n--- end ---"

        def _summarize_with_provider_retries() -> str:
            def summarize() -> str:
                return agent._run_chat_agent(prompt, "")

            retry_call = getattr(agent, "_call_with_transient_provider_retries", None)
            if callable(retry_call):
                return retry_call("compact_summary", summarize)
            return summarize()

        try:
            summary = await asyncio.get_running_loop().run_in_executor(
                None,
                _summarize_with_provider_retries,
            )
            evidence_index = _compact_exact_evidence_index(transcript)
            if evidence_index:
                summary = (summary or "").rstrip() + "\n\n" + evidence_index
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="upstream_error",
                        message=f"compact summarisation failed: {exc!r}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        # Insert the summary as a new assistant message at the head of the
        # ledger (after archiving the originals to a parallel list so a
        # future /resume can recover full history). The TUI doesn't see
        # archived messages — only the compact summary + anything that
        # comes after it.
        event_id = _new_memory_event_id()
        compacted_at = datetime.now(timezone.utc).isoformat()
        archive = app.state.__dict__.setdefault("session_archives", {})
        archive.setdefault(sid, []).append(
            {
                "compacted_at": time.time(),
                "memory_event_id": event_id,
                "messages": list(ledger),
            }
        )

        arc = getattr(agent, "arc", None)
        arc_status = "not_configured"
        if arc is not None:
            try:
                from clio_agent.arc.schema import (  # noqa: PLC0415
                    Conversation as ARCConversation,
                )
                from clio_agent.arc.schema import Message as ARCMessage  # noqa: PLC0415

                now_ts = time.time()
                arc_summary = ARCMessage(
                    role="assistant",
                    content="[compact summary]\n" + (summary or "").strip(),
                    timestamp=now_ts,
                    metadata={
                        "source": "gact_compact",
                        "synthetic": "compact_summary",
                        "memory_event_id": event_id,
                        "archived_count": len(ledger),
                    },
                )
                conv = arc.get_conversation(sid)
                if conv is None:
                    conv = ARCConversation(
                        session_id=sid,
                        user_id="default_user",
                        created_at=now_ts,
                        updated_at=now_ts,
                        last_accessed=now_ts,
                        status="active",
                        messages=[arc_summary],
                        routing_decisions=[],
                        metadata={
                            "clio_agent_version": "0.2.0",
                            "arc_enabled": True,
                            "compacted_by": "gact",
                        },
                        storage_tier="warm",
                    )
                else:
                    conv.messages = [arc_summary]
                    conv.updated_at = now_ts
                    conv.last_accessed = now_ts
                    conv.metadata["compacted_by"] = "gact"
                    conv.metadata["compacted_at"] = now_ts
                    conv.metadata["archived_message_count"] = len(ledger)
                arc.store_conversation(conv)
                arc_status = "stored"
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="memory_update_failed",
                            message=f"compact summary could not be stored in ARC memory: {exc!r}",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc

        from clio_agent.gact.types import Message, Part, Tokens  # noqa: PLC0415

        compact_message = Message(
            id=f"msg_compact_{uuid.uuid4().hex[:10]}",
            session_id=sid,
            role="assistant",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            parts=[
                Part(
                    id=f"part_compact_{uuid.uuid4().hex[:10]}",
                    type="text",
                    metadata={
                        "synthetic": "compact_summary",
                        "memory_event_id": event_id,
                    },
                    text="[compact summary]\n" + (summary or "").strip(),
                )
            ],
            tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
            cost_usd=0.0,
            stop_reason="end_turn",
            metadata={
                "synthetic": "compact_summary",
                "memory_event_id": event_id,
            },
        )
        _replace_session_messages(app, sid, [compact_message])
        memory_event = {
            "id": event_id,
            "version": 1,
            "type": "compact_summary",
            "session_id": sid,
            "created_at": compacted_at,
            "updated_at": compacted_at,
            "summary_message_id": compact_message.id,
            "archived_count": len(ledger),
            "summary_chars": len((summary or "")),
            "transcript_chars": len(transcript),
            "transcript_limit": transcript_limit,
            "per_part_limit": per_part_limit,
            "focus": focus,
            "arc_status": arc_status,
            "metadata": {
                "source": "gact_compact",
                "synthetic": "compact_summary",
                "evidence_index": "[exact retained evidence index]" in (summary or ""),
            },
        }
        app.state.memory_events.setdefault(sid, []).append(memory_event)

        # Publish so any open SSE stream redraws.
        app.state.bus.publish(
            Event(
                type="session.compacted",
                session_id=sid,
                payload={
                    "event_id": event_id,
                    "archived_count": len(ledger),
                    "summary_chars": len((summary or "")),
                    "summary_message_id": compact_message.id,
                    "version": 1,
                },
            )
        )
        return {
            "session_id": sid,
            "compacted": True,
            "event_id": event_id,
            "archived_count": len(ledger),
            "summary": summary,
        }

    @app.get("/v1/sessions/{sid}/memory/events")
    async def list_session_memory_events(sid: str, limit: int = 50) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if limit <= 0:
            limit = 50
        limit = min(limit, 200)
        events = list(app.state.memory_events.get(sid, []))
        return {"events": events[-limit:]}

    @app.get("/v1/sessions/{sid}/memory/events/{event_id}")
    async def get_session_memory_event(sid: str, event_id: str) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        event = next(
            (row for row in app.state.memory_events.get(sid, []) if row.get("id") == event_id),
            None,
        )
        if event is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"memory event not found: {event_id}",
                        details={"session_id": sid, "event_id": event_id},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return {"event": event}

    @app.delete("/v1/mcp/servers/{sid}", status_code=204)
    async def uninstall_mcp_server(sid: str) -> None:
        """Drop a third-party MCP server registration. Bundled
        in-process servers (mcp_fs/mcp_hdf5/mcp_parquet) cannot be
        removed at runtime — return 404 for those."""

        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        if sid not in installed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"no externally-installed MCP server: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        _guard_direct_destructive_action(
            app,
            tool_name="gact.mcp_server.delete",
            args={"server_id": sid},
            summary=f"uninstall MCP server {sid}",
            reason="user_requested_mcp_server_delete",
        )
        installed.pop(sid, None)
        return None

    @app.get("/v1/mcp/servers/{sid}")
    async def get_mcp_server(sid: str) -> dict[str, Any]:
        """SPEC §6.7 detail endpoint for one MCP server row."""

        for row in _mcp_server_rows():
            if row.get("id") == sid:
                return row
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"no MCP server: {sid}",
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    # ---- /v1/mcp/servers/{sid}/(tools|resources|prompts) ----------------
    # Detail enumeration for the TUI MCP browser. Bundled servers are
    # introspected via the in-process gateway; external servers via a
    # short-lived fastmcp.Client connection (same transport spec used at
    # install time).

    def _bundled_server_tools(short_name: str) -> list[dict[str, Any]]:
        """Return tools for a bundled in-process server, shaped for the
        TUI's catalog detail rows (id/name/description)."""
        try:
            from clio_agent.tools.gateway import list_capabilities

            caps = list_capabilities()
        except Exception:
            return []
        out = []
        for tool in caps:
            if tool.get("server") != short_name:
                continue
            out.append(
                {
                    "id": tool.get("name", ""),
                    "name": tool.get("name", ""),
                    "description": tool.get("description") or "",
                }
            )
        return out

    async def _external_mcp_inventory(sid: str, kind: str) -> list[dict[str, Any]]:
        """Fetch tools|resources|prompts from a third-party MCP server."""
        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        info = installed.get(sid)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"no installed MCP server: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            from fastmcp import Client
            from fastmcp.client.transports import (
                StdioTransport,
                StreamableHttpTransport,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_missing",
                        message=f"fastmcp Client unavailable: {exc!r}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        spec = info.get("spec", {})
        if spec.get("transport") == "stdio":
            transport = StdioTransport(
                command=spec["command"],
                args=spec.get("args") or [],
            )
        elif spec.get("transport") == "http":
            transport = StreamableHttpTransport(url=spec["url"])  # type: ignore[assignment]
        else:
            return []
        rows: list[dict[str, Any]] = []
        try:
            async with Client(transport) as client:
                if kind == "tools":
                    items = await client.list_tools()
                    for t in items:
                        rows.append(
                            {
                                "id": t.name,
                                "name": t.name,
                                "description": getattr(t, "description", "") or "",
                            }
                        )
                elif kind == "resources":
                    items = await client.list_resources()
                    for r in items:
                        uri = str(getattr(r, "uri", ""))
                        rows.append(
                            {
                                "id": uri or getattr(r, "name", ""),
                                "name": getattr(r, "name", "") or uri,
                                "description": getattr(r, "description", "") or "",
                            }
                        )
                elif kind == "prompts":
                    items = await client.list_prompts()
                    for p in items:
                        rows.append(
                            {
                                "id": p.name,
                                "name": p.name,
                                "description": getattr(p, "description", "") or "",
                            }
                        )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="upstream_error",
                        message=f"MCP {kind} listing failed: {exc!r}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        return rows

    @app.get("/v1/mcp/servers/{sid}/tools")
    async def get_mcp_tools(sid: str) -> dict[str, Any]:
        """List tools for an MCP server. Bundled servers report what the
        in-process gateway has registered; third-party servers connect
        via fastmcp.Client and call tools/list."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"tools": _bundled_server_tools(sid[len("mcp_") :])}
        return {"tools": await _external_mcp_inventory(sid, "tools")}

    @app.get("/v1/mcp/servers/{sid}/resources")
    async def get_mcp_resources(sid: str) -> dict[str, Any]:
        """List resources for an MCP server. Bundled servers don't
        expose resources today (return empty); external servers query
        resources/list via fastmcp.Client."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"resources": []}
        return {"resources": await _external_mcp_inventory(sid, "resources")}

    @app.get("/v1/mcp/servers/{sid}/prompts")
    async def get_mcp_prompts(sid: str) -> dict[str, Any]:
        """List prompts for an MCP server. Bundled servers don't expose
        prompts today (return empty); external servers query
        prompts/list via fastmcp.Client."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"prompts": []}
        return {"prompts": await _external_mcp_inventory(sid, "prompts")}

    # ---- /v1/sessions/{sid}/schedules (#21) --------------------------

    @app.get("/v1/sessions/{sid}/schedules")
    async def list_schedules(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = [s.to_wire() for s in app.state.schedules.list(session_id=sid)]
        return {"schedules": rows}

    @app.post("/v1/sessions/{sid}/schedules")
    async def add_schedule(sid: str, request: Request) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        cron = (body.get("cron") or "").strip()
        question = (body.get("question") or "").strip()
        if not cron or not question:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required fields: cron + question",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        sch = app.state.schedules.add(session_id=sid, cron=cron, question=question)
        return sch.to_wire()

    @app.delete("/v1/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> Response:
        sch = app.state.schedules.get(schedule_id)
        if sch is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"schedule not found: {schedule_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        sess = app.state.sessions.get(sch.session_id)
        _guard_direct_destructive_action(
            app,
            session_id=sch.session_id,
            workspace_id=getattr(sess, "workspace_id", ""),
            tool_name="gact.schedule.delete",
            args={"schedule_id": schedule_id, "session_id": sch.session_id},
            summary=f"delete schedule {schedule_id}",
            reason="user_requested_schedule_delete",
        )
        existed = app.state.schedules.delete(schedule_id)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"schedule not found: {schedule_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Response(status_code=204)

    # ---- /v1/sessions/{sid}/share + /v1/shared/{token} (#22) ---------

    @app.post("/v1/sessions/{sid}/share")
    async def share_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        ttl_s = int(body.get("ttl_s") or 0)
        token = "shr_" + uuid.uuid4().hex[:24]
        expires_at: str | float = ""
        if ttl_s > 0:
            expires_at = datetime.now(timezone.utc).timestamp() + ttl_s
        app.state.shared_tokens[token] = {
            "session_id": sid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
        }
        return {
            "token": token,
            "session_id": sid,
            "url": f"/v1/shared/{token}",
            "expires_at": expires_at,
        }

    @app.get("/v1/shared/{token}")
    async def get_shared(token: str) -> dict[str, Any]:
        row = app.state.shared_tokens.get(token)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"share token not found: {token}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Expiry check.
        expires_at = row.get("expires_at") or 0
        if expires_at and (datetime.now(timezone.utc).timestamp() > float(expires_at)):
            app.state.shared_tokens.pop(token, None)
            raise HTTPException(
                status_code=410,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="share token expired",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        sid = row["session_id"]
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(f"underlying session {sid} no longer exists"),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        msgs = app.state.messages.get(sid, [])
        return {
            "session": Session(**sess.to_wire()).model_dump(exclude_none=True),
            "messages": [m.model_dump(exclude_none=True) for m in msgs],
            "shared_at": row.get("created_at"),
        }

    # ---- /v1/agents/extract (#23) -------------------------------------

    @app.post("/v1/agents/extract", response_model=AgentDef, status_code=201)
    async def extract_agent(request: Request) -> AgentDef:
        """Extract a new dynamic agent from past sessions.

        Body: ``{session_ids: [..], agent_id: ".."}``. Walks the
        message logs of the listed sessions, harvests the most-
        common tool names called, and registers a user agent
        whose tools list reflects that pattern. Real DSPy SIMBA
        compilation is deferred — this is the heuristic baseline.
        """

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        sids = [s for s in (body.get("session_ids") or []) if isinstance(s, str)]
        new_id = (body.get("agent_id") or "").strip()
        if not sids or not new_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="required: session_ids[] + agent_id",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if new_id in {"main", "data", "analysis", "visualization"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(f"agent id {new_id!r} is built-in; pick a different one"),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        # Walk the message logs.
        from collections import Counter

        tool_counts: Counter[str] = Counter()
        sample_questions: list[str] = []
        for sid in sids:
            for m in app.state.messages.get(sid, []):
                if m.role == "user":
                    text = next(
                        (p.text for p in m.parts if p.type == "text" and p.text),
                        "",
                    )
                    if text:
                        sample_questions.append(text)
                if m.role == "assistant":
                    md = m.metadata or {}
                    for call in md.get("tools_called", []) or []:
                        name = (
                            call.get("name")
                            if isinstance(call, dict)
                            else getattr(call, "name", "")
                        )
                        if name:
                            tool_counts[name] += 1
        top_tools = [t for t, _ in tool_counts.most_common(5)]
        keywords = sorted(
            {w.strip(".,").lower() for q in sample_questions[:5] for w in q.split() if len(w) >= 4}
        )[:8]
        payload = {
            "id": new_id,
            "title": f"Extracted from {len(sids)} session(s)",
            "description": (
                f"Auto-extracted agent from {len(sids)} session log(s). "
                f"Common tools: {', '.join(top_tools) if top_tools else '(none)'}"
            ),
            "tier": 2,
            "specialization": "extracted",
            "keywords": keywords,
            "tools": top_tools,
        }
        agent = app.state.user_agents.upsert(payload)
        return AgentDef(**agent.to_wire())

    # ---- /v1/sessions/{sid}/export + /v1/sessions/import (#16) -------

    @app.get("/v1/sessions/{sid}/export")
    async def export_session(sid: str) -> dict[str, Any]:
        """SPEC §6.x — dump a session + its messages as a single
        portable JSON blob. Useful for sharing analyses, archiving,
        replay. Round-trips through POST /v1/sessions/import.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        msgs = app.state.messages.get(sid, [])
        ws = app.state.workspaces.get(sess.workspace_id)
        return {
            "version": "1",
            "session": Session(**sess.to_wire()).model_dump(exclude_none=True),
            "workspace": (Workspace(**ws.to_wire()).model_dump(exclude_none=True) if ws else None),
            "messages": [m.model_dump(exclude_none=True) for m in msgs],
            "context_files": [
                dict(row) for row in app.state.context_files.get(sid, {}).values()
            ],
        }

    @app.post("/v1/sessions/import", response_model=Session)
    async def import_session(blob: dict[str, Any]) -> Session:
        """Restore a session from an export blob. Creates a fresh
        session in ws_default (or the workspace named in the blob
        if it exists locally) and re-plays the messages as already-
        settled rows. Returns the new Session row.
        """

        sess_data = blob.get("session", {})
        title = sess_data.get("title") or "imported"
        wid = "ws_default"
        if blob.get("workspace") and app.state.workspaces.get(blob["workspace"].get("id", "")):
            wid = blob["workspace"]["id"]
        new_sess = app.state.sessions.create(
            workspace_id=wid,
            title=title,
            metadata=sess_data.get("metadata") or {},
        )
        msg_rows: list[Message] = []
        for m in blob.get("messages", []):
            try:
                msg = Message(**{**m, "session_id": new_sess.id})
                msg_rows.append(msg)
            except Exception:
                continue
        _replace_session_messages(app, new_sess.id, msg_rows)
        context_files: dict[str, dict[str, Any]] = {}
        for row in blob.get("context_files", []):
            if not isinstance(row, Mapping):
                continue
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            context_files[path] = dict(row)
        if context_files:
            app.state.context_files[new_sess.id] = context_files
        cost_total = sum(float(m.get("cost_usd", 0.0) or 0.0) for m in blob.get("messages", []))
        in_total = sum(
            int((m.get("tokens") or {}).get("input", 0) or 0) for m in blob.get("messages", [])
        )
        out_total = sum(
            int((m.get("tokens") or {}).get("output", 0) or 0) for m in blob.get("messages", [])
        )
        app.state.sessions.update(
            new_sess.id,
            message_count=len(msg_rows),
            add_tokens_input=in_total,
            add_tokens_output=out_total,
            add_cost_usd=cost_total,
        )
        refreshed = app.state.sessions.get(new_sess.id)
        return Session(**refreshed.to_wire())

    # ---- GET /v1/sessions/{sid}/messages/search (BBB27) ---------------

    @app.get("/v1/sessions/{sid}/messages/search")
    async def search_messages(sid: str, q: str = "") -> dict[str, Any]:
        """Case-insensitive substring search across stored messages.

        Returns ``{matches: [{message_id, part_id, snippet, score}]}``.
        Score is a crude recency-biased ranking: newer hits score
        higher (+0.01 per message index) so identical snippets
        surface in turn order.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        needle = q.strip().lower()
        if not needle:
            return {"matches": []}

        matches: list[dict[str, Any]] = []
        rows = app.state.messages.get(sid, [])
        for idx, m in enumerate(rows):
            for part in m.parts:
                text = (part.text or "").lower()
                i = text.find(needle)
                if i < 0:
                    continue
                # 60-char snippet window centered on the hit.
                start = max(0, i - 30)
                end = min(len(part.text), i + len(needle) + 30)
                snippet = part.text[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(part.text):
                    snippet = snippet + "…"
                matches.append(
                    {
                        "message_id": m.id,
                        "part_id": part.id,
                        "snippet": snippet,
                        "score": 1.0 + (idx * 0.01),
                    }
                )
        matches.sort(key=lambda r: r["score"], reverse=True)
        return {"matches": matches}

    # ---- Ask-user and retry protocol (#333) --------------------------

    def _session_not_found(sid: str) -> HTTPException:
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="internal_error",
                    message=f"session not found: {sid}",
                    details={"session_id": sid},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    def _question_not_found(sid: str, question_id: str) -> HTTPException:
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"user question not found: {question_id}",
                    details={"session_id": sid, "question_id": question_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    def _pending_user_questions(sid: str) -> list[UserQuestion]:
        return [
            q
            for q in app.state.user_questions.values()
            if q.session_id == sid and q.status == "pending"
        ]

    def _set_session_status(
        sid: str,
        status: str,
        *,
        prev_status: str = "",
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> None:
        updated = app.state.sessions.update(
            sid,
            status=status,
            metadata_patch=metadata_patch,
        )
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "status": status,
                    "prev_status": prev_status,
                    "updated_at": updated.updated_at if updated is not None else "",
                },
            )
        )

    def _normalize_question_options(
        req: CreateUserQuestionRequest,
    ) -> list[UserQuestionOption]:
        if req.kind == "confirmation" and not req.options:
            return [
                UserQuestionOption(label="Yes", value="yes", description=""),
                UserQuestionOption(label="No", value="no", description=""),
            ]
        return list(req.options)

    def _start_background_user_turn(
        sid: str,
        sess: Session,
        user_text: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
        prev_status: str = "idle",
        turn_agent_id: str = "",
    ) -> Message:
        now = time.time()
        user_metadata = dict(metadata or {})
        if turn_agent_id:
            user_metadata["agent_override"] = {
                "requested_agent_id": turn_agent_id,
                "session_agent_id": _session_agent_id(sess),
                "scope": "turn",
            }
        user_msg = Message(
            id=_new_message_id("user"),
            session_id=sid,
            role="user",
            created_at=_iso_from_epoch(now),
            updated_at=_iso_from_epoch(now),
            parts=[Part(id=_new_part_id(), type="text", text=user_text)],
            metadata=user_metadata,
        )

        _append_session_message(app, sid, user_msg)
        app.state.sessions.update(sid, status="running")
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "status": "running",
                    "prev_status": prev_status,
                },
            )
        )
        app.state.bus.publish(
            Event(
                type="message.created",
                session_id=sid,
                payload=user_msg.model_dump(exclude_none=True),
            )
        )

        task = asyncio.create_task(
            _run_turn_in_background(app, sid, user_text, user_msg, turn_agent_id)
        )
        app.state.in_flight_turns[sid] = task

        def _drop_task(_t, _sid=sid) -> None:
            cur = app.state.in_flight_turns.get(_sid)
            if cur is _t:
                app.state.in_flight_turns.pop(_sid, None)

        task.add_done_callback(_drop_task)
        return user_msg

    def _message_text(message: Message) -> str:
        return "\n".join(
            part.text for part in message.parts if part.type == "text" and part.text
        ).strip()

    def _retry_source_user_message(messages: list[Message], source: Message) -> Message | None:
        if source.role == "user":
            return source
        try:
            source_index = next(idx for idx, msg in enumerate(messages) if msg.id == source.id)
        except StopIteration:
            return None
        for msg in reversed(messages[:source_index]):
            if msg.role == "user":
                return msg
        return None

    def _retry_user_text(original_text: str, notes: str) -> str:
        notes = notes.strip()
        if not notes:
            return original_text
        return f"{original_text}\n\n[Retry notes]\n{notes}"

    @app.get("/v1/sessions/{sid}/questions")
    async def list_user_questions(sid: str, status: str = "") -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        rows = [q for q in app.state.user_questions.values() if q.session_id == sid]
        if status:
            rows = [q for q in rows if q.status == status]
        rows.sort(key=lambda q: q.created_at, reverse=True)
        return {"questions": [q.model_dump(exclude_none=True) for q in rows]}

    @app.post("/v1/sessions/{sid}/questions", response_model=UserQuestion, status_code=201)
    async def create_user_question(
        sid: str,
        req: CreateUserQuestionRequest,
    ) -> UserQuestion:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        prompt = req.prompt.strip()
        if not prompt:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="missing required field: prompt",
                        details={"field": "prompt"},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        row = UserQuestion(
            id=_new_question_id(),
            session_id=sid,
            prompt=prompt,
            kind=req.kind,
            options=_normalize_question_options(req),
            created_at=now_iso,
            updated_at=now_iso,
            expires_at=req.expires_at,
            source=req.source or "orchestrator",
            turn_id=req.turn_id,
            attempt_id=req.attempt_id,
            metadata=req.metadata,
        )
        app.state.user_questions[row.id] = row
        _set_session_status(
            sid,
            "waiting_user",
            prev_status=sess.status,
            metadata_patch={"pending_user_question_id": row.id},
        )
        app.state.bus.publish(
            Event(
                type="user_question.created",
                session_id=sid,
                payload=row.model_dump(exclude_none=True),
            )
        )
        return row

    @app.post("/v1/sessions/{sid}/questions/{question_id}/answer", response_model=UserQuestion)
    async def answer_user_question(
        sid: str,
        question_id: str,
        req: AnswerUserQuestionRequest,
    ) -> UserQuestion:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        row = app.state.user_questions.get(question_id)
        if row is None or row.session_id != sid:
            raise _question_not_found(sid, question_id)
        if row.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=f"user question is already {row.status}",
                        details={"session_id": sid, "question_id": question_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        allowed_values = {o.value or o.label for o in row.options}
        selected = [s for s in req.selected_options if s]
        if allowed_values and selected and any(s not in allowed_values for s in selected):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="selected option is not valid for this question",
                        details={
                            "session_id": sid,
                            "question_id": question_id,
                            "allowed": sorted(allowed_values),
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        updated = row.model_copy(
            update={
                "status": "answered",
                "answer": req.answer,
                "selected_options": selected,
                "answer_metadata": req.metadata,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        app.state.user_questions[question_id] = updated
        if not _pending_user_questions(sid):
            sess = app.state.sessions.get(sid)
            _set_session_status(
                sid,
                "idle",
                prev_status=sess.status if sess is not None else "waiting_user",
                metadata_patch={"pending_user_question_id": ""},
            )
        app.state.bus.publish(
            Event(
                type="user_question.answered",
                session_id=sid,
                payload=updated.model_dump(exclude_none=True),
            )
        )
        return updated

    @app.post("/v1/sessions/{sid}/questions/{question_id}/cancel", response_model=UserQuestion)
    async def cancel_user_question(sid: str, question_id: str) -> UserQuestion:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        row = app.state.user_questions.get(question_id)
        if row is None or row.session_id != sid:
            raise _question_not_found(sid, question_id)
        if row.status == "pending":
            row = row.model_copy(
                update={
                    "status": "cancelled",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            app.state.user_questions[question_id] = row
        if not _pending_user_questions(sid):
            sess = app.state.sessions.get(sid)
            _set_session_status(
                sid,
                "idle",
                prev_status=sess.status if sess is not None else "waiting_user",
                metadata_patch={"pending_user_question_id": ""},
            )
        app.state.bus.publish(
            Event(
                type="user_question.cancelled",
                session_id=sid,
                payload=row.model_dump(exclude_none=True),
            )
        )
        return row

    @app.get("/v1/sessions/{sid}/attempts")
    async def list_turn_attempts(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        rows = [a for a in app.state.turn_attempts.values() if a.session_id == sid]
        rows.sort(key=lambda a: a.created_at, reverse=True)
        return {"attempts": [a.model_dump(exclude_none=True) for a in rows]}

    @app.post(
        "/v1/sessions/{sid}/messages/{message_id}/retry",
        response_model=TurnAttempt,
        status_code=202,
    )
    async def retry_turn(sid: str, message_id: str, req: RetryTurnRequest) -> TurnAttempt:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        messages = app.state.messages.get(sid, [])
        source = next((m for m in messages if m.id == message_id), None)
        if source is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found: {message_id}",
                        details={"session_id": sid, "message_id": message_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        model_payload = (req.model or ModelRef()).model_dump()
        if req.provider_id:
            model_payload["provider_id"] = req.provider_id
        if req.model_id:
            model_payload["model_id"] = req.model_id
        active_model = _active_lm_model_ref(app)
        model_changed = bool(
            (model_payload.get("provider_id") or model_payload.get("model_id"))
            and (
                model_payload.get("provider_id", "") != active_model.get("provider_id", "")
                or model_payload.get("model_id", "") != active_model.get("model_id", "")
            )
        )
        warning = ""
        if model_changed:
            warning = (
                "Retrying with a different model/provider may recompute provider-side KV "
                "cache, increase time to first token, increase latency/cost, and produce "
                "different tool or reasoning behavior."
            )
        execution_blocked_reason = ""
        retry_user_msg: Message | None = None
        source_user = _retry_source_user_message(messages, source)
        if req.execute:
            if app.state.agent is None:
                raise HTTPException(
                    status_code=503,
                    detail=_agent_not_available_error(app, sid).model_dump(exclude_none=True),
                )
            lm_status = getattr(app.state, "lm_config_status", {}) or {}
            if lm_status.get("state") == "configuring":
                raise HTTPException(
                    status_code=503,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="provider_configuring",
                            message=(
                                "LM provider configuration is still in progress; retry after "
                                "it finishes."
                            ),
                            details={
                                "session_id": sid,
                                "operation_id": lm_status.get("operation_id", ""),
                                "provider": lm_status.get("provider", ""),
                                "model": lm_status.get("model", ""),
                                "recovery_actions": [
                                    "wait",
                                    "check_lm_provider_status",
                                    "retry",
                                ],
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            if source_user is None or not _message_text(source_user):
                execution_blocked_reason = "source_user_message_not_found"
            elif model_changed:
                execution_blocked_reason = "model_override_not_executable"
        now_iso = datetime.now(timezone.utc).isoformat()
        attempt = TurnAttempt(
            id=_new_attempt_id(),
            session_id=sid,
            source_message_id=message_id,
            status=(
                "queued"
                if req.execute and not execution_blocked_reason
                else ("failed" if req.execute else "recorded")
            ),
            created_at=now_iso,
            updated_at=now_iso,
            notes=req.notes,
            model=ModelRef(**model_payload),
            warning=warning,
            metadata={
                **req.metadata,
                "source_message_role": source.role,
                "source_user_message_id": source_user.id if source_user is not None else "",
                "active_model": active_model,
                "retry_protocol": "queued_for_execution" if req.execute else "recorded_for_replay",
                "execution_blocked_reason": execution_blocked_reason,
            },
        )
        app.state.turn_attempts[attempt.id] = attempt
        if req.execute and not execution_blocked_reason and source_user is not None:
            retry_text = _retry_user_text(_message_text(source_user), req.notes)
            retry_user_msg = _start_background_user_turn(
                sid,
                sess,
                retry_text,
                metadata={
                    "retry_attempt_id": attempt.id,
                    "retry_source_message_id": message_id,
                    "retry_source_user_message_id": source_user.id,
                    "retry_notes": req.notes,
                    **req.metadata,
                },
                prev_status=sess.status,
            )
            attempt = attempt.model_copy(
                update={
                    "metadata": {
                        **attempt.metadata,
                        "queued_user_message_id": retry_user_msg.id,
                    }
                }
            )
            app.state.turn_attempts[attempt.id] = attempt
        app.state.bus.publish(
            Event(
                type="turn.retry_requested",
                session_id=sid,
                payload=attempt.model_dump(exclude_none=True),
            )
        )
        return attempt

    # ---- POST /v1/sessions/{sid}/cancel (BBB20) -----------------------

    @app.post("/v1/sessions/{sid}/cancel")
    async def cancel_session(sid: str) -> Response:
        """Best-effort cancel of an in-flight turn on this session.

        The agent loop and sync MCP bridge observe a scoped cancellation
        checker between planner/expert/tool boundaries and return early
        with ``error_info.error == "cancelled"`` when possible. The
        endpoint itself flips the flag + publishes a
        ``session.cancelled`` event so any live SSE subscriber sees
        the transition without waiting for the next turn boundary.

        If the turn is already blocked inside executor-thread provider
        or tool work, cancelling the asyncio Task settles the GACT
        envelope as cancelled but cannot kill the underlying Python
        thread. The emitted status event marks this as best-effort so
        clients do not mistake it for a guaranteed provider abort.

        Returns 204 whether a turn was actually running — the TUI
        fires this on Esc/Ctrl+C speculatively and doesn't want an
        error if the race finished on its own.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        # Set the cancellation flag. Cooperative agent/tool paths check
        # it between expensive boundaries; the turn handler also checks
        # it after forward() returns so non-cooperative agents still
        # produce a truthful cancelled envelope.
        app.state.cancel_flags.add(sid)
        event = app.state.cancel_events.get(sid)
        if event is not None:
            event.set()
        in_flight = app.state.in_flight_turns.get(sid)
        cancellation_pending = False
        if in_flight is not None and not in_flight.done():
            cancellation_pending = True
        attempt = {
            "id": _new_cancellation_attempt_id(),
            "session_id": sid,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "in_flight": cancellation_pending,
            "cooperative_signal_sent": event is not None,
            "asyncio_task_cancel_scheduled": cancellation_pending,
            "asyncio_task_cancel_sent": False,
            "hard_abort_supported": False,
            "upstream_abort": "not_supported",
            "executor_work_may_continue": cancellation_pending,
        }
        app.state.cancel_attempts[sid] = attempt
        if cancellation_pending:

            async def _cancel_after_grace(task: asyncio.Task, session_id: str) -> None:
                await asyncio.sleep(0.1)
                if session_id in app.state.cancel_flags and not task.done():
                    latest_attempt = app.state.cancel_attempts.get(session_id)
                    if latest_attempt is attempt:
                        attempt["asyncio_task_cancel_sent"] = True
                        attempt["asyncio_task_cancelled_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                    task.cancel()

            asyncio.create_task(_cancel_after_grace(in_flight, sid))
        app.state.sessions.update(sid, status="cancelled")
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "status": "cancelled",
                    "prev_status": sess.status,
                    "execution_cancellation": (
                        "cooperative_pending" if cancellation_pending else "none"
                    ),
                    "executor_work_may_continue": cancellation_pending,
                    "cancellation_attempt": _cancellation_attempt_summary(attempt),
                },
            )
        )
        return Response(status_code=204)

    # ---- POST /v1/sessions/{sid}/messages (BBB9) ---------------------
    # Non-streaming turn: 1 request, 1 response body containing both
    # the stored user message + the assistant's reply. Streaming
    # (SSE on /v1/sessions/{sid}/events) lands in BBB10.

    @app.post("/v1/sessions/{sid}/messages", response_model=PostMessageResponse)
    async def post_message(
        sid: str, req: PostMessageRequest, background_tasks: BackgroundTasks
    ) -> PostMessageResponse:
        """Accept a user message and ack immediately. The agent turn
        runs in the background; clients consume progress via the SSE
        channel (message.created, message.part.delta, ..., message.completed).

        Returning early matters: real LM turns can run for minutes
        (DSPy ReAct loops × 5-15s per Claude call). Holding the POST
        connection open for the whole turn means TUI timeouts, broken
        streaming UX, and no way to surface progress to the user.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        lm_status = getattr(app.state, "lm_config_status", {}) or {}
        if lm_status.get("state") == "configuring":
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="provider_configuring",
                        message=(
                            "LM provider configuration is still in progress; retry after it "
                            "finishes."
                        ),
                        details={
                            "session_id": sid,
                            "operation_id": lm_status.get("operation_id", ""),
                            "provider": lm_status.get("provider", ""),
                            "model": lm_status.get("model", ""),
                            "recovery_actions": ["wait", "check_lm_provider_status", "retry"],
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.agent is None:
            raise HTTPException(
                status_code=503,
                detail=_agent_not_available_error(app, sid).model_dump(exclude_none=True),
            )

        if (
            req.model is not None
            and not _model_ref_is_empty(req.model)
            and not _model_ref_matches_active(req.model, app)
        ):
            active_model = _active_lm_model_ref(app)
            raise HTTPException(
                status_code=501,
                detail=_unsupported_model_ref_error(
                    session_id=sid,
                    source="per_message",
                    model_ref=req.model,
                    active_model=active_model,
                ).model_dump(exclude_none=True),
            )

        if not _model_ref_is_empty(sess.model) and not _model_ref_matches_active(sess.model, app):
            active_model = _active_lm_model_ref(app)
            if active_model.get("model_id"):
                app.state.sessions.update(sid, model={})
                sess = app.state.sessions.get(sid) or sess
            else:
                raise HTTPException(
                    status_code=501,
                    detail=_unsupported_model_ref_error(
                        session_id=sid,
                        source="session",
                        model_ref=sess.model,
                        active_model=active_model,
                    ).model_dump(exclude_none=True),
                )

        user_text = req.extract_text()
        turn_agent_id = req.extract_agent_id().strip()
        if not user_text:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(
                            "request body carried no text: expected "
                            "parts[] containing a text part or legacy "
                            "top-level text field"
                        ),
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Persist + publish the user message synchronously so by the
        # time the ack returns, GET /messages reflects it. Then mark
        # the session running, then schedule the turn in the
        # background and return.
        user_msg = _start_background_user_turn(
            sid,
            sess,
            user_text,
            metadata=req.metadata,
            prev_status="idle",
            turn_agent_id=turn_agent_id,
        )
        # background_tasks parameter is unused but kept on the
        # signature so existing callers (and FastAPI's docs) don't
        # change shape.
        del background_tasks

        return PostMessageResponse(
            message_id=user_msg.id,
            accepted_at=user_msg.created_at,
        )

    @app.get("/v1/sessions/{sid}/messages")
    async def list_messages(sid: str) -> dict[str, Any]:
        """List messages in a session.

        Today: in-memory log populated by POST /messages; returns
        empty when the session exists but has no turns yet. The v0.1
        wire shape (no pagination header, bare array) is what every
        v0.1 backend does; v0.2 clients accept both.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # TUI (and SPEC §6.4) expect newest-first with an optional
        # cursor for older pages. We store chronologically so reverse
        # at read time.
        rows = list(reversed(app.state.messages.get(sid, [])))
        return {
            "messages": [m.model_dump(exclude_none=True) for m in rows],
            "next_cursor": None,
        }

    @app.get("/v1/sessions/{sid}/messages/{message_id}")
    async def get_message(sid: str, message_id: str) -> dict[str, Any]:
        """SPEC §6.3 drill-down for one stored message."""

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        for msg in app.state.messages.get(sid, []):
            if msg.id == message_id:
                return msg.model_dump(exclude_none=True)
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"message not found: {message_id}",
                    details={"session_id": sid, "message_id": message_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    # ---- /v1/agents catalog (BBB10) + dynamic registry (#19) ---------

    def _agent_with_capability_refs(agent_def: AgentDef) -> AgentDef:
        """Attach normalized capability metadata to an AgentDef row."""

        refs: list[AgentCapabilityRef] = [
            AgentCapabilityRef(kind="tool", id=tool_id, title=tool_id, source="builtin")
            for tool_id in agent_def.tools
        ]
        refs.extend(
            AgentCapabilityRef(kind="skill", id=skill_id, title=skill_id, source=agent_def.source)
            for skill_id in agent_def.skills
        )
        refs.extend(
            AgentCapabilityRef(
                kind="command",
                id=command_id,
                title=command_id,
                source="builtin",
            )
            for command_id in agent_def.commands
        )
        refs.extend(agent_def.capability_refs)

        if agent_def.id == "main":
            command_ids = set(agent_def.commands)
            for row in _BACKEND_COMMANDS:
                command_id = row["id"]
                if command_id in command_ids:
                    continue
                raw_status = row.get("status")
                status: Literal["available", "unavailable", "unknown"] = (
                    raw_status
                    if raw_status in {"available", "unavailable", "unknown"}
                    else "available"
                )
                refs.append(
                    AgentCapabilityRef(
                        kind="command",
                        id=command_id,
                        title=row.get("title", command_id),
                        description=row.get("description", ""),
                        source=row.get("source", "builtin"),
                        status=status,
                        metadata=(
                            {"error": row["error"]}
                            if row.get("error")
                            else {}
                        ),
                    )
                )
                command_ids.add(command_id)
            agent_def = agent_def.model_copy(update={"commands": sorted(command_ids)})

        if agent_def.source == "skill" and agent_def.id not in agent_def.skills:
            refs.append(
                AgentCapabilityRef(
                    kind="skill",
                    id=agent_def.id,
                    title=agent_def.title,
                    description=agent_def.description,
                    source=str(agent_def.metadata.get("skill_source", "skill")),
                    metadata={
                        "skill_path": agent_def.metadata.get("skill_path", ""),
                        "skill_layout": agent_def.metadata.get("skill_layout", ""),
                    },
                )
            )
            agent_def = agent_def.model_copy(update={"skills": [*agent_def.skills, agent_def.id]})

        deduped: list[AgentCapabilityRef] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.kind, ref.id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ref)

        return agent_def.model_copy(update={"capability_refs": deduped})

    def _workspace_catalog_cwd(workspace_id: str = "", session_id: str = "") -> Path | None:
        wid = workspace_id
        if session_id:
            sess = app.state.sessions.get(session_id)
            if sess is not None:
                wid = wid or str(getattr(sess, "workspace_id", "") or "")
        if not wid:
            return None
        ws = app.state.workspaces.get(wid)
        if ws is None:
            return None
        root_path = str(getattr(ws, "root_path", "") or "")
        return Path(root_path).expanduser() if root_path else None

    def _active_session_expert_pack_id(session_id: str = "") -> str:
        if not session_id:
            return ""
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return ""
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return ""
        return str(metadata.get("active_expert_pack_id") or metadata.get("expert_pack_id") or "").strip()

    def _active_session_expert_pack_path(session_id: str = "") -> Path | None:
        if not session_id:
            return None
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return None
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return None
        raw = str(metadata.get("active_expert_pack_path") or "").strip()
        return Path(raw).expanduser() if raw else None

    def _agent_rows(session_id: str = "", workspace_id: str = "") -> list[AgentDef]:
        cwd = _workspace_catalog_cwd(workspace_id=workspace_id, session_id=session_id)
        active_pack_id = _active_session_expert_pack_id(session_id)
        active_pack_path = _active_session_expert_pack_path(session_id)
        explicit_session_rows = (
            load_expert_pack_path(active_pack_path, scope="session")
            if active_pack_path is not None
            else []
        )
        rows = (
            _builtin_agents()
            + [AgentDef(**row.to_wire()) for row in app.state.user_agents.list()]
            + _load_skills_from_disk()
            + load_expert_packs(cwd=cwd, pack_id=active_pack_id)
            + explicit_session_rows
        )
        return [
            _apply_prompt_registry_to_agent(app, _agent_with_capability_refs(row))
            for row in validate_expert_hierarchy(_merge_agent_def_rows(rows))
        ]

    @app.get("/v1/expert-packs")
    async def list_expert_packs(workspace_id: Optional[str] = None) -> dict[str, Any]:
        cwd = _workspace_catalog_cwd(workspace_id=workspace_id or "")
        packs = [pack.to_wire() for pack in discover_expert_packs(cwd=cwd)]
        return {"expert_packs": packs}

    @app.get("/v1/expert-packs/{pack_id:path}")
    async def get_expert_pack(pack_id: str, workspace_id: Optional[str] = None) -> dict[str, Any]:
        cwd = _workspace_catalog_cwd(workspace_id=workspace_id or "")
        for pack in discover_expert_packs(cwd=cwd):
            if pack.id == pack_id:
                agents = validate_expert_hierarchy(load_expert_packs(cwd=cwd, pack_id=pack_id))
                return {
                    "expert_pack": pack.to_wire(),
                    "agents": [row.model_dump(exclude_none=True) for row in agents],
                }
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"expert pack not found: {pack_id}",
                    details={"pack_id": pack_id, "workspace_id": workspace_id or ""},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.post("/v1/expert-packs/validate")
    async def validate_expert_pack(req: dict[str, Any]) -> dict[str, Any]:
        path = str(req.get("path") or "").strip()
        if not path:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="path is required",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return validate_expert_pack_path(Path(path), scope=str(req.get("scope") or "session"))

    @app.get("/v1/sessions/{sid}/expert-pack")
    async def get_session_expert_pack(sid: str) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        pack_id = _active_session_expert_pack_id(sid)
        pack_path = _active_session_expert_pack_path(sid)
        cwd = _workspace_catalog_cwd(session_id=sid)
        pack = next((row for row in discover_expert_packs(cwd=cwd) if row.id == pack_id), None)
        pack_wire: dict[str, Any] | None = pack.to_wire() if pack is not None else None
        if pack is None and pack_path is not None:
            validation = validate_expert_pack_path(pack_path, scope="session")
            raw_pack = validation.get("pack")
            pack_wire = raw_pack if isinstance(raw_pack, dict) else None
        return {
            "session_id": sid,
            "workspace_id": getattr(sess, "workspace_id", ""),
            "active_expert_pack_id": pack_id,
            "active_expert_pack_path": str(pack_path) if pack_path is not None else "",
            "expert_pack": pack_wire,
        }

    @app.post("/v1/sessions/{sid}/expert-pack")
    async def set_session_expert_pack(sid: str, req: dict[str, Any]) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        pack_id = str(req.get("pack_id") or "").strip()
        pack_path = str(req.get("path") or req.get("pack_path") or "").strip()
        cwd = _workspace_catalog_cwd(session_id=sid)
        if pack_path:
            validation = validate_expert_pack_path(Path(pack_path), scope="session")
            if not validation.get("enabled", False):
                raise HTTPException(
                    status_code=400,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="validation_error",
                            message="expert pack path is invalid",
                            details={
                                "path": pack_path,
                                "validation_errors": validation.get("validation_errors", []),
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            pack_wire = validation["pack"]
            updated = app.state.sessions.update(
                sid,
                metadata_patch={
                    "active_expert_pack_id": str(pack_wire.get("id") or ""),
                    "active_expert_pack_version": str(pack_wire.get("version") or ""),
                    "active_expert_pack_scope": "session",
                    "active_expert_pack_definition_path": str(pack_wire.get("definition_path") or ""),
                    "active_expert_pack_path": str(Path(pack_path).expanduser()),
                },
            )
            _mirror_workspace_session(app, sid)
            return {
                "session_id": sid,
                "workspace_id": getattr(sess, "workspace_id", ""),
                "active_expert_pack_id": str(pack_wire.get("id") or ""),
                "active_expert_pack_path": str(Path(pack_path).expanduser()),
                "expert_pack": pack_wire,
                "session": Session(**updated.to_wire()).model_dump(exclude_none=True) if updated else None,
            }
        if not pack_id:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="pack_id or path is required",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        pack = next((row for row in discover_expert_packs(cwd=cwd) if row.id == pack_id), None)
        if pack is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"expert pack not found: {pack_id}",
                        details={"pack_id": pack_id, "session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        updated = app.state.sessions.update(
            sid,
            metadata_patch={
                "active_expert_pack_id": pack.id,
                "active_expert_pack_version": pack.version,
                "active_expert_pack_scope": pack.scope,
                "active_expert_pack_definition_path": str(pack.manifest_path or pack.root),
                "active_expert_pack_path": "",
            },
        )
        _mirror_workspace_session(app, sid)
        return {
            "session_id": sid,
            "workspace_id": getattr(sess, "workspace_id", ""),
            "active_expert_pack_id": pack.id,
            "expert_pack": pack.to_wire(),
            "session": Session(**updated.to_wire()).model_dump(exclude_none=True) if updated else None,
        }

    @app.get("/v1/agents", response_model=ListAgentsResponse)
    async def list_agents(
        tier: Optional[int] = None,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> ListAgentsResponse:
        """SPEC §6.5 + v0.2 §4.3.1: optional ?tier=N filter.

        Combines built-in tier-1/2 experts with any user-registered
        agents (iowarp/clio-agent#19). Built-ins always come first
        so the TUI's sidebar groups consistently.
        """

        rows = _agent_rows(session_id=session_id or "", workspace_id=workspace_id or "")
        if tier is not None:
            rows = [a for a in rows if a.tier == tier]
        return ListAgentsResponse(agents=rows)

    @app.get("/v1/agents/{agent_id}", response_model=AgentDef)
    async def get_agent(agent_id: str) -> AgentDef:
        """SPEC §6.5 detail endpoint for built-in/user/skill agents."""

        for row in _agent_rows():
            if row.id == agent_id:
                return row
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"agent not found: {agent_id}",
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.post("/v1/agents", response_model=AgentDef, status_code=201)
    async def create_agent(req: dict[str, Any]) -> AgentDef:
        """iowarp/clio-agent#19: register a new dynamic agent.

        The agent is stored as an AgentDef row + persisted to disk;
        future GET /v1/agents calls include it. Built-in id collision
        is rejected so users can't shadow CLIO's core experts.
        Source is forced to "user" regardless of what the client sent.
        """

        agent_id = req.get("id", "")
        if agent_id in {"main", "data", "analysis", "visualization"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(
                            f"agent id {agent_id!r} is reserved for a "
                            "built-in expert; pick a different id"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if not agent_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: id",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        # Force user-source so a malicious client can't claim builtin.
        req = dict(req)
        req["source"] = "user"
        agent = app.state.user_agents.upsert(req)
        return _agent_with_capability_refs(AgentDef(**agent.to_wire()))

    @app.put("/v1/agents/{agent_id}", response_model=AgentDef)
    async def update_agent(agent_id: str, req: dict[str, Any]) -> AgentDef:
        """Replace an existing user agent. Built-ins are immutable."""

        if agent_id in {"main", "data", "analysis", "visualization"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(
                            f"agent id {agent_id!r} is a built-in; "
                            "rebuild CLIO to change its definition"
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.user_agents.get(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"agent not found: {agent_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Force the URL id to win over the body to avoid the user
        # silently renaming via PUT. Force user source.
        body = dict(req)
        body["id"] = agent_id
        body["source"] = "user"
        agent = app.state.user_agents.upsert(body)
        return _agent_with_capability_refs(AgentDef(**agent.to_wire()))

    @app.delete("/v1/agents/{agent_id}")
    async def delete_agent(agent_id: str) -> Response:
        """Drop a user-registered agent. Built-ins are immutable."""

        if agent_id in {"main", "data", "analysis", "visualization"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(f"agent id {agent_id!r} is a built-in and cannot be removed"),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.user_agents.get(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"agent not found: {agent_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        _guard_direct_destructive_action(
            app,
            tool_name="gact.agent.delete",
            args={"agent_id": agent_id},
            summary=f"delete agent {agent_id}",
            reason="user_requested_agent_delete",
        )
        app.state.user_agents.delete(agent_id)
        return Response(status_code=204)

    @app.get("/v1/catalog/tools", response_model=ListToolsResponse)
    async def list_tools() -> ListToolsResponse:
        return ListToolsResponse(tools=_builtin_tools())

    def _estimate_message_context_tokens(message: Message) -> int:
        explicit = (
            int(getattr(message.tokens, "input", 0) or 0)
            + int(getattr(message.tokens, "output", 0) or 0)
            + int(getattr(message.tokens, "cache_read", 0) or 0)
            + int(getattr(message.tokens, "cache_write", 0) or 0)
        )
        if explicit > 0:
            return explicit
        chars = 0
        for part in message.parts:
            chars += len(part.text or "")
            chars += len(str(getattr(part, "thinking", "") or ""))
            chars += len(part.path or "")
            chars += len(part.unified_diff or "")
            chars += len(part.new_content or "")
        return max(1, chars // 4) if chars else 0

    def _estimate_context_file_tokens(row: Mapping[str, Any]) -> int:
        size = row.get("size")
        try:
            raw_size = int(size or 0)
        except (TypeError, ValueError):
            raw_size = 0
        # Context-file injection caps inlined bodies at _CTX_MAX_BYTES.
        retained_bytes = min(max(raw_size, 0), _CTX_MAX_BYTES)
        return retained_bytes // 4

    def _context_pressure_state(
        tokens_retained: int,
        tokens_budget: int,
    ) -> tuple[float, Literal["empty", "normal", "warning", "critical"], bool]:
        if tokens_budget <= 0:
            return 0.0, "empty" if tokens_retained == 0 else "normal", False
        pressure = min(1.0, tokens_retained / tokens_budget)
        if tokens_retained <= 0:
            return 0.0, "empty", False
        if pressure >= 0.9:
            return pressure, "critical", True
        if pressure >= 0.75:
            return pressure, "warning", True
        return pressure, "normal", False

    # ---- /v1/memory/stats (BBB11) ------------------------------------
    # Returns cache counters + per-session context retention + global
    # ARC totals. When ARC isn't wired (tests, smoke-boot scenarios)
    # returns zeros per SPEC §6.19 ("zeros are a valid signal").

    @app.get(
        "/v1/memory/stats",
        response_model=MemoryStats,
        response_model_by_alias=True,
    )
    async def memory_stats(session_id: Optional[str] = None) -> MemoryStats:
        if app.state.arc is not None:
            raw = app.state.arc.get_cache_stats()
            cache = CacheStats(
                hits=int(raw.get("hits", 0)),
                misses=int(raw.get("misses", 0)),
                hit_rate=float(raw.get("hit_rate", 0.0)),
                capacity=int(raw.get("capacity", 0)),
            )
            # ARC tracks conversation + invocation counts via the
            # index sizes it reports alongside the cache. Future: if
            # the numbers start diverging from what operators expect
            # we can call dedicated getters; for now the index sizes
            # are a good-faith approximation.
            global_stats = GlobalMemoryStats(
                conversations_total=int(raw.get("conv_index_size", 0)),
                invocations_total=int(raw.get("inv_index_size", 0)),
            )
        else:
            cache = CacheStats()
            global_stats = GlobalMemoryStats()

        session_block: Optional[SessionMemoryStats] = None
        metadata: dict[str, Any] = {
            "retained_context_source": "visible_gact_transcript",
            "token_estimate": "message_tokens_or_chars_div_4",
        }
        if session_id:
            sess_rec = app.state.sessions.get(session_id)
            if sess_rec is not None:
                messages = list(app.state.messages.get(session_id, []))
                context_files = list((app.state.context_files.get(session_id, {}) or {}).values())
                context_files_by_mode: dict[str, int] = {"edit": 0, "pin": 0, "read": 0}
                for row in context_files:
                    mode = str(row.get("mode") or "read")
                    context_files_by_mode[mode] = context_files_by_mode.get(mode, 0) + 1
                transcript_tokens = sum(_estimate_message_context_tokens(m) for m in messages)
                context_file_tokens = sum(_estimate_context_file_tokens(row) for row in context_files)
                tokens_retained = transcript_tokens + context_file_tokens
                tokens_budget = 4000
                pressure, threshold_state, compact_recommended = _context_pressure_state(
                    tokens_retained,
                    tokens_budget,
                )
                compact_summaries = sum(
                    1
                    for m in messages
                    if m.metadata.get("synthetic") == "compact_summary"
                    or any(p.metadata.get("synthetic") == "compact_summary" for p in m.parts)
                )
                session_block = SessionMemoryStats(
                    session_id=session_id,
                    messages_retained=len(messages),
                    tokens_retained=tokens_retained,
                    tokens_budget=tokens_budget,
                    profiles_attached=0,
                    context_files_attached=len(context_files),
                    context_files_by_mode=context_files_by_mode,
                    compact_summaries=compact_summaries,
                    token_pressure=pressure,
                    threshold_state=threshold_state,
                    compaction_recommended=compact_recommended,
                )
                metadata["session"] = {
                    "transcript_tokens": transcript_tokens,
                    "context_file_tokens": context_file_tokens,
                    "recorded_lifetime_tokens": sess_rec.tokens_input + sess_rec.tokens_output,
                }
            else:
                # Unknown session: return an empty block rather than
                # a 404. The TUI's footer chip handles zero stats
                # gracefully; a 404 would spam the logs on every
                # mis-timed fetch.
                session_block = SessionMemoryStats(session_id=session_id)

        return MemoryStats(
            cache=cache,
            session=session_block,
            global_=global_stats,  # type: ignore[call-arg]  # Pydantic alias "global"
            metadata=metadata,
        )

    @app.get("/v1/memory/search", response_model=MemorySearchResponse)
    async def memory_search(
        query: str,
        session_id: str = "",
        workspace_id: str = "",
        include_cross_session: bool = False,
        limit: int = 20,
    ) -> MemorySearchResponse:
        """Search retained transcript memory.

        Normal calls are session-scoped. Cross-session search is intentionally
        opt-in so future orchestrator tools can support "based on the last few
        days" without silently leaking unrelated sessions into every turn.
        """

        return _memory_search_response(
            app,
            query=query,
            session_id=session_id,
            workspace_id=workspace_id,
            include_cross_session=include_cross_session,
            limit=limit,
        )

    # ---- /v1/sessions/{sid}/events SSE (BBB13) -----------------------

    @app.get("/v1/sessions/{sid}/events")
    async def session_events(sid: str, request: Request) -> StreamingResponse:
        """SSE feed for one session. Emits the events POST /messages
        publishes (status_changed, message.created, message.part.*,
        message.completed) plus periodic 15-s heartbeats so HTTP
        proxies don't drop the idle connection.

        Per SPEC §7.1: streams forever until the client disconnects.
        Emits ``server.connected`` immediately so clients can confirm
        the wire is healthy before any real event arrives.
        """

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        async def event_stream() -> AsyncIterator[bytes]:
            # Initial server.connected event so clients can flip
            # their UI from "connecting" to "live" immediately.
            connected = Event(
                type="server.connected",
                session_id=sid,
                payload={"server_version": GACT_BACKEND_VERSION},
            )
            yield _format_sse(connected)

            try:
                last_event_id = int(request.headers.get("last-event-id", "0"))
            except (TypeError, ValueError):
                last_event_id = 0
            sub = app.state.bus.subscribe(sid, last_event_id=last_event_id)
            heartbeat_task: Optional[asyncio.Task] = None
            try:
                # Heartbeat task — pumps a server.heartbeat event
                # into the queue every 15s. SPEC §7.1.
                async def _heartbeat() -> None:
                    while True:
                        await asyncio.sleep(15)
                        app.state.bus.publish(
                            Event(
                                type="server.heartbeat",
                                session_id=sid,
                                payload=heartbeat_payload(),
                            )
                        )

                heartbeat_task = asyncio.create_task(_heartbeat())

                async for event in sub:
                    yield _format_sse(event)
            except asyncio.CancelledError:
                # Client disconnected. Cleanup happens in `finally`.
                pass
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
            },
        )

    # ---- /v1/metrics (BBB15) -----------------------------------------

    @app.get("/v1/metrics", response_model=Metrics)
    async def metrics() -> Metrics:
        """Aggregate runtime metrics — SPEC §6.16.

        Today: counters synthesised from the session + in-memory
        message logs. ARC-backed per-expert latency/success-rate
        rollups come in when we reshape `ARCMemory.get_metrics()`
        into this envelope (tracked in the v0.3 roadmap); for now
        the endpoint returns the wire-compatible skeleton with zero
        tokens/cost/latencies so the TUI's Metrics tab renders
        rather than falling back to a permanent "n/a".
        """

        uptime = max(0, int(time.time() - app.state.started_at))

        all_sessions = app.state.sessions.list()
        by_status: dict[str, int] = {}
        active = 0
        for s in all_sessions:
            by_status[s.status] = by_status.get(s.status, 0) + 1
            if s.status in {"running", "idle"}:
                active += 1

        message_total = 0
        role_counts: dict[str, int] = {}
        for rows in app.state.messages.values():
            message_total += len(rows)
            for m in rows:
                role_counts[m.role] = role_counts.get(m.role, 0) + 1

        # CLIO-BBBBBBBBBB24: tokens + cost rollup across every
        # session's cumulative counters.
        from clio_agent.gact.types import MetricsCost, MetricsTokens

        tokens_input = sum(s.tokens_input for s in all_sessions)
        tokens_output = sum(s.tokens_output for s in all_sessions)
        cost_total = sum(s.cost_usd for s in all_sessions)

        return Metrics(
            uptime_s=uptime,
            sessions=MetricsSessions(
                total=len(all_sessions),
                active=active,
                by_status=by_status,
            ),
            messages=MetricsMessages(
                total=message_total,
                by_role=role_counts,
            ),
            tokens=MetricsTokens(
                input_total=tokens_input,
                output_total=tokens_output,
            ),
            cost=MetricsCost(total_usd=cost_total),
        )

    # ---- /v1/workspaces (CLIO-BBBBBBBBBB-WS) -------------------------

    @app.get("/v1/workspaces", response_model=ListWorkspacesResponse)
    async def list_workspaces() -> ListWorkspacesResponse:
        """SPEC §6.1 — list workspaces."""

        rows = app.state.workspaces.list()
        return ListWorkspacesResponse(workspaces=[Workspace(**w.to_wire()) for w in rows])

    @app.post("/v1/workspaces", response_model=Workspace, status_code=201)
    async def create_workspace(req: CreateWorkspaceRequest) -> Workspace:
        """SPEC §6.1 — create a workspace pinned to ``root_path``."""

        ws = app.state.workspaces.create(
            name=req.name,
            root_path=req.root_path,
            storage_root=req.storage_root,
            metadata=req.metadata,
        )
        return Workspace(**ws.to_wire())

    @app.get("/v1/workspaces/{wid}", response_model=Workspace)
    async def get_workspace(wid: str) -> Workspace:
        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Workspace(**ws.to_wire())

    @app.delete("/v1/workspaces/{wid}")
    async def delete_workspace(wid: str) -> Response:
        """Refuses to delete ws_default — every CLIO install needs
        one workspace alive so sessions have a parent."""

        if wid == "ws_default":
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message="ws_default is not deletable",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.workspaces.get(wid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        _guard_direct_destructive_action(
            app,
            workspace_id=wid,
            tool_name="gact.workspace.delete",
            args={"workspace_id": wid},
            summary=f"delete workspace {wid}",
            reason="user_requested_workspace_delete",
        )
        app.state.workspaces.delete(wid)
        return Response(status_code=204)

    # ---- /v1/workspaces/{wid}/files (gact-tui @-picker) -------------
    #
    # gact-tui's `@`-trigger file picker calls
    # /v1/workspaces/{wid}/files expecting a flat list of FileEntry
    # rooted at the workspace's root_path. Until this endpoint existed
    # the picker rendered as 404 ("file-picker: gact: 404"). We walk
    # the workspace root, skip cost-walking dirs (.git, __pycache__,
    # node_modules, .venv, build/), respect the file policy's
    # allow-symlinks flag, and cap at _FILE_PICKER_LIMIT entries so a
    # giant repo doesn't lock the picker for seconds while the
    # filesystem walk runs.
    _FILE_PICKER_LIMIT = 5000
    _FILE_PICKER_SKIP_DIRS = {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".npm",
        ".venv",
        "venv",
        ".tox",
        "build",
        "dist",
        ".egg-info",
        ".clio_agent",  # ARC's local persistence
    }

    @app.get("/v1/workspaces/{wid}/files")
    async def list_workspace_files(wid: str) -> dict[str, Any]:
        """SPEC §6.9 — list files under a workspace's root_path.

        Returns ``{"entries": [{"path", "type", "size", "modified"}, …]}``
        with paths relative to root_path so the TUI can show short
        labels. Type is "file" or "dir"; the picker filters dirs
        client-side. Hard-capped at _FILE_PICKER_LIMIT to keep large
        repos from blocking the modal.
        """

        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        root = Path(ws.root_path or os.getcwd()).expanduser()
        if not root.is_dir():
            return {"entries": []}

        # File policy decides whether symlinks are walkable; everything
        # else (size cap, allowed-roots) is enforced at read-time, not
        # listing-time.
        allow_symlinks = False
        try:
            from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

            policy = FileAccessPolicy.from_mapping(os.environ)
            allow_symlinks = policy.allow_symlinks
        except Exception:
            pass

        entries: list[dict[str, Any]] = []
        cap = _FILE_PICKER_LIMIT

        def _walk(d: Path) -> None:
            nonlocal cap
            if cap <= 0:
                return
            try:
                raw_children = list(d.iterdir())
            except (OSError, PermissionError):
                return
            # Don't stat-sort up front — a single un-statable child
            # (broken symlink, restricted unix socket in /tmp) raises
            # mid-key-eval and drops the entire list. Sort by name only;
            # we'll check is_dir per-entry behind a try.
            raw_children.sort(key=lambda p: p.name)
            for child in raw_children:
                if cap <= 0:
                    return
                name = child.name
                if name in _FILE_PICKER_SKIP_DIRS:
                    continue
                try:
                    if child.is_symlink() and not allow_symlinks:
                        continue
                    is_dir = child.is_dir()
                except OSError:
                    # Unreadable entry — skip rather than abort the whole
                    # walk. Common in /tmp where other users' sockets
                    # are 0600 and trip stat's permission check.
                    continue
                rel = str(child.relative_to(root))
                entry: dict[str, Any] = {
                    "path": rel,
                    "type": "dir" if is_dir else "file",
                }
                if not is_dir:
                    try:
                        st = child.stat()
                        entry["size"] = st.st_size
                        entry["modified"] = (
                            datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                    except OSError:
                        pass
                entries.append(entry)
                cap -= 1
                if is_dir:
                    _walk(child)

        _walk(root)
        return {"entries": entries}

    @app.get("/v1/workspaces/{wid}/repo_map")
    async def workspace_repo_map(wid: str) -> dict[str, Any]:
        """SPEC §6.9 repo-map envelope for the workspace file tree.

        The map intentionally reuses the capped file picker walk so a
        large repository cannot turn the read-only contract endpoint
        into an unbounded filesystem scan.
        """

        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        root = Path(ws.root_path or os.getcwd()).expanduser()
        tree: dict[str, Any] = {
            "name": root.name or str(root),
            "path": "",
            "type": "dir",
            "children": [],
        }
        body = await list_workspace_files(wid)
        entries = body.get("entries", [])
        nodes_by_path: dict[str, dict[str, Any]] = {"": tree}
        token_estimate = 0
        for entry in entries:
            path = str(entry.get("path") or "")
            if not path:
                continue
            normalized = path.replace("\\", "/")
            parent_key = "/".join(normalized.split("/")[:-1])
            parent = nodes_by_path.get(parent_key, tree)
            node = {
                "name": normalized.split("/")[-1],
                "path": normalized,
                "type": entry.get("type") or "file",
            }
            if node["type"] == "dir":
                node["children"] = []
            size = entry.get("size")
            if isinstance(size, int):
                node["size"] = size
                token_estimate += max(1, size // 4)
            parent.setdefault("children", []).append(node)
            nodes_by_path[normalized] = node
        return {
            "tree": tree,
            "tokens": token_estimate,
            "truncated": len(entries) >= _FILE_PICKER_LIMIT,
        }

    @app.get("/v1/workspaces/{wid}/files/read")
    async def read_workspace_file(wid: str, path: str) -> Response:
        """SPEC §6.9 — read one file's content.

        Serves the raw bytes (text/plain) so the TUI's preview panel
        can render code without a base64 decode. Refuses paths that
        escape the workspace root (``..`` segments) and paths beyond
        the file policy's max_file_size_bytes.
        """

        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        root = Path(ws.root_path or os.getcwd()).expanduser().resolve()
        try:
            target = (root / path).resolve()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_path",
                        message=f"could not resolve path: {path}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        # Refuse path-traversal: target must be at-or-below root.
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="path_outside_workspace",
                        message=f"path escapes workspace: {path}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from None
        if not target.is_file():
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"file not found: {path}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Enforce file-policy size cap so a 50 GB log doesn't OOM.
        try:
            from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

            policy = FileAccessPolicy.from_mapping(os.environ)
            max_bytes = policy.max_file_size_bytes
        except Exception:
            max_bytes = 1024 * 1024 * 1024  # 1 GiB fallback
        size = target.stat().st_size
        if size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="file_too_large",
                        message=f"file exceeds policy cap ({size} > {max_bytes} bytes)",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            data = target.read_bytes()
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="read_failed",
                        message=f"could not read file: {exc}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        return Response(
            content=data.decode("utf-8", errors="replace"),
            media_type="text/plain; charset=utf-8",
        )

    # ---- /v1/providers/lm (CLIO-BBBBBBBBBB-D) ------------------------

    # Derived from clio_agent.providers.registry. Add new presets to
    # the registry, not here — this list reflects whatever the registry
    # contains at build_app() time. Polaris preset removed for the time
    # being — the inference-api gateway returns 400 'cluster polaris
    # does not exist' for /resource_server/polaris/vllm/v1.
    from clio_agent.providers.registry import as_lm_presets as _build_lm_presets

    _LM_PRESETS: list[LMProviderPreset] = _build_lm_presets()

    def _preset_api_key_env(preset: LMProviderPreset) -> str:
        if preset.api_key_env:
            return preset.api_key_env
        return {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }.get(preset.id, "CLIO_LM_API_KEY")

    def _which_cli(*names: str) -> str | None:
        """Resolve a local CLI across POSIX names and Windows shims."""

        for name in names:
            found = shutil.which(name)
            if found:
                return found
            if os.name == "nt" and not name.lower().endswith((".cmd", ".exe")):
                for suffix in (".cmd", ".exe"):
                    found = shutil.which(name + suffix)
                    if found:
                        return found
        return None

    def _preset_with_status(preset: LMProviderPreset) -> LMProviderPreset:
        update: dict[str, Any] = {}
        if preset.provider == "argonne":
            env_token = (
                os.environ.get("CLIO_ARGONNE_TOKEN", "").strip()
                or os.environ.get("ALCF_INFERENCE_TOKEN", "").strip()
                or os.environ.get("access_token", "").strip()
            )
            if env_token:
                update["status"] = "ready"
                update["status_message"] = "ALCF token present in environment"
                update["is_authenticated"] = True
                return preset.model_copy(update=update)
            try:
                from clio_agent.providers import argonne_auth  # noqa: PLC0415
            except Exception as exc:
                update["status"] = "unavailable"
                update["status_message"] = f"argonne auth unavailable: {exc}"
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            if not argonne_auth.tokens_exist():
                update["status"] = "auth_required"
                update["status_message"] = (
                    "no Globus token stored; authenticate ALCF before connecting"
                )
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            if argonne_auth.check_auth_status():
                update["status"] = "ready"
                update["status_message"] = "Globus token validated"
                update["is_authenticated"] = True
                return preset.model_copy(update=update)
            update["status"] = "auth_required"
            update["status_message"] = (
                "stored Globus token could not be refreshed; authenticate ALCF"
            )
            update["is_authenticated"] = False
            return preset.model_copy(update=update)
        if preset.requires_api_key:
            env_key = _preset_api_key_env(preset)
            if not (os.environ.get(env_key) or os.environ.get("CLIO_LM_API_KEY")):
                update["status"] = "missing_key"
                update["status_message"] = f"missing {env_key}"
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            update["is_authenticated"] = True
        if preset.provider == "codex":
            if _which_cli("codex"):
                update["status"] = "ready"
                update["status_message"] = "codex CLI available"
                update["is_authenticated"] = True
            else:
                update["status"] = "unavailable"
                update["status_message"] = "codex CLI not found on PATH"
                update["is_authenticated"] = False
            return preset.model_copy(update=update)
        if preset.provider == "claude_code":
            if _which_cli("claude"):
                update["status"] = "ready"
                update["status_message"] = "claude CLI available"
                update["is_authenticated"] = True
            else:
                update["status"] = "unavailable"
                update["status_message"] = "claude CLI not found on PATH"
                update["is_authenticated"] = False
            return preset.model_copy(update=update)
        if not preset.supports_live_catalog:
            update["status"] = "ready"
            update["status_message"] = "static catalog"
            update["is_authenticated"] = True
            return preset.model_copy(update=update)
        update["status"] = "unknown"
        update["status_message"] = ""
        update.setdefault("is_authenticated", not preset.requires_api_key)
        return preset.model_copy(update=update)

    def _lm_presets_with_status() -> list[LMProviderPreset]:
        return sorted(
            (_preset_with_status(preset) for preset in _LM_PRESETS),
            key=lambda p: p.label.lower(),
        )

    def _lm_provider_status() -> dict[str, Any]:
        status = getattr(app.state, "lm_config_status", None)
        if not isinstance(status, dict):
            return {"state": "idle"}
        return status

    def _lm_provider_info(*, presets: list[LMProviderPreset] | None = None) -> LMProviderInfo:
        cfg = _effective_lm_config(app)
        status = _lm_provider_status()
        state = str(status.get("state") or "idle")
        if state not in {"idle", "configuring", "ready", "error"}:
            state = "idle"
        pending = status if state == "configuring" else {}
        return LMProviderInfo(
            configured=app.state.agent is not None and state != "configuring",
            provider=str(pending.get("provider") or cfg.get("provider", "")),
            api_base=str(pending.get("api_base") or cfg.get("api_base", "")),
            model=str(pending.get("model") or cfg.get("model", "")),
            temperature=(
                float(pending["temperature"])
                if pending.get("temperature") is not None
                else float(cfg["temperature"])
                if cfg.get("temperature") is not None
                else 1.0
            ),
            max_tokens=(
                int(pending["max_tokens"])
                if pending.get("max_tokens") is not None
                else int(cfg["max_tokens"])
                if cfg.get("max_tokens") is not None
                else 32000
            ),
            context_length=(
                int(pending["context_length"])
                if pending.get("context_length") is not None
                else int(cfg["context_length"])
                if cfg.get("context_length") is not None
                else 0
            ),
            thinking_budget=(
                int(pending["thinking_budget"])
                if pending.get("thinking_budget") is not None
                else int(cfg["thinking_budget"])
                if cfg.get("thinking_budget") is not None
                else 0
            ),
            transport=pending.get("transport") or cfg.get("transport"),
            state=state,  # type: ignore[arg-type]
            status_message=str(status.get("message") or ""),
            error=str(status.get("error") or ""),
            operation_id=str(status.get("operation_id") or ""),
            presets=presets if presets is not None else _lm_presets_with_status(),
        )

    @app.get("/v1/providers/lm", response_model=LMProviderInfo)
    async def get_lm_provider() -> LMProviderInfo:
        """Report the live LM config — what we'd report on /doctor as
        the 'lm' integration row, plus a list of presets the TUI's
        provider picker shows.

        ``configured`` is true when an agent is wired and ready to
        run; the TUI uses this to decide whether to show the config
        modal on connect.
        """

        return _lm_provider_info()

    async def _apply_lm_provider(req: LMProviderRequest) -> LMProviderInfo:
        """Reconfigure the LM in-place. Rebuilds DSPy + the
        ClioAgent so subsequent POST /messages drive the new
        provider. The old agent's state (ARC, sessions, in-flight
        messages) is preserved across the swap.
        """

        env_keys = (
            "CLIO_LM_PROVIDER",
            "CLIO_LM_API_BASE",
            "CLIO_LM_MODEL",
            "CLIO_LM_API_KEY",
            "CLIO_CODEX_TRANSPORT",
        )
        env_before = {key: os.environ.get(key) for key in env_keys}
        dspy_settings_before: dict[str, Any] | None = None
        settings_sentinel = object()

        def _restore_process_env() -> None:
            for key, value in env_before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        def _restore_dspy_settings() -> None:
            if dspy_settings_before is None:
                return
            try:
                from dspy.dsp.utils.settings import main_thread_config  # noqa: PLC0415
            except Exception:
                return
            for key, value in dspy_settings_before.items():
                if value is settings_sentinel:
                    main_thread_config.pop(key, None)
                else:
                    main_thread_config[key] = value

        def _stamp_process_env(cfg: "LMProviderConfig", api_key: str) -> None:
            os.environ["CLIO_LM_PROVIDER"] = req.provider
            os.environ["CLIO_LM_API_BASE"] = req.api_base
            os.environ["CLIO_LM_MODEL"] = req.model
            os.environ["CLIO_LM_API_KEY"] = api_key
            if req.provider == "codex":
                os.environ["CLIO_CODEX_TRANSPORT"] = cfg.codex_transport
            else:
                os.environ.pop("CLIO_CODEX_TRANSPORT", None)

        def _apply_lm_studio_load_config() -> None:
            """Apply LM Studio load-time options before wiring DSPy."""

            if req.provider != "lm_studio" or req.context_length <= 0:
                return

            import requests  # noqa: PLC0415

            root = _lm_studio_api_root(req.api_base)
            if not root:
                raise RuntimeError("LM Studio api_base is empty")

            headers = _lm_studio_headers()

            def _already_loaded_with_requested_context() -> str:
                try:
                    response = requests.get(
                        f"{root}/api/v1/models",
                        headers=headers,
                        timeout=10,
                    )
                    if response.status_code >= 400:
                        return ""
                    payload = response.json()
                except Exception:
                    return ""

                models = payload.get("models")
                if not isinstance(models, list):
                    return ""
                for item in models:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key") or "")
                    loaded = item.get("loaded_instances")
                    if not isinstance(loaded, list):
                        continue
                    for instance in loaded:
                        if not isinstance(instance, dict):
                            continue
                        instance_id = str(instance.get("id") or "")
                        if req.model not in {key, instance_id}:
                            continue
                        config = instance.get("config")
                        if not isinstance(config, dict):
                            continue
                        try:
                            loaded_context = int(config.get("context_length") or 0)
                        except (TypeError, ValueError):
                            loaded_context = 0
                        if loaded_context == req.context_length:
                            return instance_id
                return ""

            loaded_instance_id = _already_loaded_with_requested_context()
            if loaded_instance_id:
                _release_owned_lm_studio_instance(
                    app,
                    skip_instance_id=loaded_instance_id,
                    raise_on_error=True,
                )
                return

            _release_owned_lm_studio_instance(app, raise_on_error=True)
            response = requests.post(
                f"{root}/api/v1/models/load",
                headers=headers,
                json={
                    "model": req.model,
                    "context_length": req.context_length,
                    "echo_load_config": True,
                },
                timeout=180,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    "LM Studio model load failed "
                    f"({response.status_code}): {(response.text or '')[:300]}"
                )
            try:
                payload = response.json()
            except Exception:
                payload = {}
            instance_id = str(payload.get("instance_id") or "").strip()
            if instance_id:
                app.state.lm_studio_owned_instance = {
                    "root": root,
                    "instance_id": instance_id,
                    "model": req.model,
                    "context_length": req.context_length,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

        try:
            import dspy

            from clio_agent.agent import ClioAgent
            from clio_agent.config import (
                LMProviderConfig,
                create_lm,
            )

            try:
                from dspy.dsp.utils.settings import main_thread_config  # noqa: PLC0415

                dspy_settings_before = {
                    "lm": main_thread_config.get("lm", settings_sentinel),
                    "adapter": main_thread_config.get("adapter", settings_sentinel),
                }
            except Exception:
                dspy_settings_before = None

            # Argonne / ALCF: if the TUI didn't ship an api_key, mint
            # one from the user's stored Globus session. ``LMProviderConfig``
            # will do this lazily inside __post_init__ too, but we resolve
            # eagerly here so the env mirror below carries the real token
            # for ClioAgent's reconstruction (load_config_from_env reads
            # CLIO_LM_API_KEY first, before LMProviderConfig defaults run).
            resolved_api_key = req.api_key
            if req.provider == "argonne" and _is_placeholder_api_key(resolved_api_key):
                auth_exc: Exception | None
                try:
                    resolved_api_key = _resolve_argonne_runtime_api_key()
                except Exception as exc:
                    resolved_api_key = ""
                    auth_exc = exc
                else:
                    auth_exc = None
                if not resolved_api_key:
                    raise HTTPException(
                        status_code=401,
                        detail=ErrorEnvelope(
                            error=ErrorInfo(
                                error="argonne_auth_required",
                                message=(
                                    "ALCF provider selected but no Globus token "
                                    "is available. Run "
                                    "`python -m clio_agent.providers.argonne_auth "
                                    "authenticate` once, or pass api_key in this "
                                    "request."
                                ),
                                recoverable=True,
                            )
                        ).model_dump(exclude_none=True),
                    ) from auth_exc

            cfg = LMProviderConfig(
                provider=req.provider,  # type: ignore[arg-type]  # str validated at boundary
                api_base=req.api_base,
                model=req.model,
                api_key=resolved_api_key or "x",
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                thinking_budget=req.thinking_budget,
                codex_transport=req.transport or "exec",
            )
            await asyncio.get_running_loop().run_in_executor(
                None,
                _apply_lm_studio_load_config,
            )
            # iowarp/clio-agent — DSPy 3.x forbids dspy.configure()
            # being re-called from a different async task than the
            # first one. PUT /v1/providers/lm comes from the FastAPI
            # request task, never the boot task, so the second call
            # always blew up. Side-step the guard by mutating
            # ``settings.main_thread_config['lm']`` directly — same
            # underlying state DSPy's __getattr__ reads, no async
            # task ownership check.
            new_lm = create_lm(cfg)
            from clio_agent.config import (  # noqa: PLC0415
                create_chat_adapter,
                create_planner_lm,
            )

            new_adapter = create_chat_adapter(cfg)
            new_planner_lm = create_planner_lm(cfg)
            try:
                from dspy.dsp.utils.settings import main_thread_config  # noqa: PLC0415

                main_thread_config["lm"] = new_lm
                main_thread_config["adapter"] = new_adapter
            except Exception:  # pragma: no cover - dspy missing
                dspy.configure(lm=new_lm, adapter=new_adapter)
            # Hot-swap the LM on the existing agent instead of
            # rebuilding from scratch. ClioAgent's expensive state
            # (ARC retriever, LSM tree, registry, expert instances,
            # tool gateways) is LM-independent — rebuilding it for
            # every Save+Connect costs ~5-10 s and is exactly the
            # latency the user complained about. These attribute
            # swaps cover the LM-dependent surface:
            #   * _provider_config   -> health/config surfaces the new provider
            #   * _main_lm           -> chat + answer synthesis use the new lm
            #   * _planner_lm        -> planner runs with the new lm
            #   * _dspy_adapter      -> local backends keep text ChatAdapter mode
            #   * dspy.settings.lm   -> experts pick it up via dspy.context()
            # Only rebuild from scratch when no agent yet exists
            # (first-connect lifecycle: the deferred-construction
            # task hasn't completed).
            existing = app.state.agent
            if existing is not None:
                existing._provider_config = cfg
                existing._main_lm = new_lm
                existing._planner_lm = new_planner_lm
                existing._router_lm = new_planner_lm
                existing._dspy_adapter = new_adapter
                agent = existing
            else:
                # First-time agent construction still reads the provider from
                # env; restore the snapshot if construction rejects it.
                _stamp_process_env(cfg, resolved_api_key or "x")
                agent = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ClioAgent(verbose=False)
                )
        except HTTPException:
            # Argonne auth path raises a structured 401 above; keep its
            # error code intact instead of flattening to a generic 400.
            _restore_process_env()
            _restore_dspy_settings()
            raise
        except Exception as exc:  # noqa: BLE001
            _restore_process_env()
            _restore_dspy_settings()
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="config_error",
                        message=f"failed to configure LM: {exc}",
                        details={"original_error": type(exc).__name__},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        # Swap the agent + ARC atomically. Old agent isn't
        # explicitly closed because we don't know what background
        # state it owns; Python's GC will clean up.
        _stamp_process_env(cfg, resolved_api_key or "x")
        app.state.agent = agent
        app.state.arc = agent.arc
        app.state.lm_config = {
            "provider": req.provider,
            "api_base": req.api_base,
            "model": req.model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "context_length": req.context_length,
            "thinking_budget": req.thinking_budget,
            "transport": cfg.codex_transport if req.provider == "codex" else None,
        }
        _clear_session_model_refs(app)
        # Publish so live SSE subscribers see the swap (TUI updates
        # its model chip without polling).
        app.state.bus.publish(
            Event(
                type="lm.provider.changed",
                session_id="",
                payload={
                    "provider": req.provider,
                    "model": req.model,
                    "api_base": req.api_base,
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "context_length": req.context_length,
                    "transport": cfg.codex_transport if req.provider == "codex" else None,
                },
            )
        )
        return LMProviderInfo(
            configured=True,
            provider=req.provider,
            api_base=req.api_base,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            context_length=req.context_length,
            thinking_budget=req.thinking_budget,
            transport=cfg.codex_transport if req.provider == "codex" else None,
            presets=_lm_presets_with_status(),
        )

    async def _run_lm_provider_apply(req: LMProviderRequest, operation_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(
                None,
                lambda: asyncio.run(_apply_lm_provider(req)),
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict):
                err = detail.get("error")
                if isinstance(err, dict):
                    error_code = str(err.get("error") or "config_error")
                    message = str(err.get("message") or exc)
                else:
                    error_code = "config_error"
                    message = str(detail)
            else:
                error_code = "config_error"
                message = str(detail or exc)
            app.state.lm_config_status = {
                "state": "error",
                "operation_id": operation_id,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "error": error_code,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            app.state.bus.publish(
                Event(
                    type="lm.provider.failed",
                    session_id="",
                    payload={
                        "operation_id": operation_id,
                        "provider": req.provider,
                        "model": req.model,
                        "error": error_code,
                        "message": message,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            app.state.lm_config_status = {
                "state": "error",
                "operation_id": operation_id,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "error": "config_error",
                "message": f"failed to configure LM: {exc}",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            app.state.bus.publish(
                Event(
                    type="lm.provider.failed",
                    session_id="",
                    payload={
                        "operation_id": operation_id,
                        "provider": req.provider,
                        "model": req.model,
                        "error": "config_error",
                        "message": f"failed to configure LM: {exc}",
                    },
                )
            )
        else:
            app.state.lm_config_status = {
                "state": "ready",
                "operation_id": operation_id,
                "provider": info.provider,
                "api_base": info.api_base,
                "model": info.model,
                "temperature": info.temperature,
                "max_tokens": info.max_tokens,
                "context_length": info.context_length,
                "thinking_budget": info.thinking_budget,
                "transport": info.transport,
                "message": "LM provider ready",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    @app.put("/v1/providers/lm", response_model=LMProviderInfo)
    async def put_lm_provider(req: LMProviderRequest) -> LMProviderInfo:
        """Start or perform an LM provider swap without freezing the backend."""

        running_task = getattr(app.state, "lm_config_task", None)
        if running_task is not None and not running_task.done():
            status = _lm_provider_status()
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="provider_configuring",
                        message="LM provider configuration is already in progress.",
                        details={
                            "operation_id": status.get("operation_id", ""),
                            "provider": status.get("provider", ""),
                            "model": status.get("model", ""),
                            "recovery_actions": ["wait", "check_lm_provider_status"],
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # LM Studio model loads/context changes and ALCF Globus token
        # refresh/provider wiring can block long enough to make the
        # selector feel frozen. Run those swaps in the background so
        # capability, health, agent catalog, and provider-selector
        # requests stay responsive.
        if req.provider in {"lm_studio", "argonne"}:
            operation_id = f"lmcfg_{uuid.uuid4().hex[:12]}"
            provider_label = "LM Studio" if req.provider == "lm_studio" else "ALCF"
            app.state.lm_config_status = {
                "state": "configuring",
                "operation_id": operation_id,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
                "context_length": req.context_length,
                "thinking_budget": req.thinking_budget,
                "transport": req.transport,
                "message": f"{provider_label} provider configuration is in progress.",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            task = asyncio.create_task(_run_lm_provider_apply(req, operation_id))
            app.state.lm_config_task = task
            return _lm_provider_info()

        info = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: asyncio.run(_apply_lm_provider(req)),
        )
        app.state.lm_config_status = {
            "state": "ready",
            "operation_id": "",
            "provider": info.provider,
            "api_base": info.api_base,
            "model": info.model,
            "temperature": info.temperature,
            "max_tokens": info.max_tokens,
            "context_length": info.context_length,
            "thinking_budget": info.thinking_budget,
            "transport": info.transport,
            "message": "LM provider ready",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return _lm_provider_info(presets=info.presets)

    @app.get("/v1/providers/{provider_id}")
    async def get_provider(provider_id: str) -> dict[str, Any]:
        """SPEC §6.12 detail endpoint for one provider preset."""

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        details={"available": [p.id for p in _LM_PRESETS]},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return _provider_to_wire(preset)

    # ---- 501 stubs for the still-unwired v0.2 surface ----------------

    _stub_routes: list[tuple[str, str, str]] = [
        # (method, path, capability_name_for_error)
        # /v1/tools moved out of stubs — implemented below.
    ]

    # ---- /v1/tools (unified catalog across all MCP servers) ----------
    # Aggregates bundled (in_process) + installed (third-party) MCP
    # servers into a single flat list keyed by tool name. Each row
    # carries the source server id so the TUI can group/filter.
    @app.get("/v1/tools")
    async def list_tools_unified() -> dict[str, Any]:
        """SPEC §6.5 — unified tool catalog.

        Walks every MCP server the backend has mounted (bundled fs/
        hdf5/parquet via the in-process gateway, plus any third-party
        servers installed via POST /v1/mcp/servers) and returns a
        single flat list of tools. Each tool row carries:
        - id / name: the tool name (namespaced where the gateway
          namespaces them, e.g. "fs_read_file")
        - description: from the tool's docstring or schema
        - server_id / source: which MCP server exposes it
        - input_schema: JSON Schema (when available)
        """
        rows: list[dict[str, Any]] = []
        # Bundled in-process tools.
        try:
            from clio_agent.tools.gateway import list_gateway_tools  # noqa: PLC0415

            for tool in await list_gateway_tools():
                srv = tool.get("server", "")
                tool_name = tool.get("name", "")
                rows.append(
                    {
                        "id": tool_name,
                        "name": tool_name,
                        "description": tool.get("description") or "",
                        "server_id": f"mcp_{srv}" if srv else "",
                        "source": "mcp",
                        "input_schema": tool.get("input_schema") or {},
                        "output_schema": tool.get("output_schema") or {},
                        "permission_default": "ask",
                        "owner": _tool_owner_for_catalog(tool_name),
                        "tags": _tool_tags_for_catalog(tool_name),
                        "visible_to": _tool_visible_to_for_catalog(tool_name),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "id": "_bundled_error",
                    "name": "_bundled_error",
                    "description": f"bundled gateway introspection failed: {exc!r}",
                    "source": "error",
                }
            )

        # Third-party installed servers — query each via fastmcp.Client.
        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        if installed:
            try:
                from fastmcp import Client  # noqa: PLC0415
                from fastmcp.client.transports import (  # noqa: PLC0415
                    StdioTransport,
                    StreamableHttpTransport,
                )
            except Exception:  # noqa: BLE001
                Client = None  # type: ignore
            for sid, info in sorted(installed.items()):
                spec = info.get("spec", {})
                if Client is None:
                    continue
                if spec.get("transport") == "stdio":
                    transport = StdioTransport(
                        command=spec["command"],
                        args=spec.get("args") or [],
                    )
                elif spec.get("transport") == "http":
                    transport = StreamableHttpTransport(url=spec["url"])  # type: ignore[assignment]
                else:
                    continue
                try:
                    async with Client(transport) as client:
                        tools = await client.list_tools()
                    for t in tools:
                        tool_name = t.name
                        rows.append(
                            {
                                "id": tool_name,
                                "name": tool_name,
                                "description": getattr(t, "description", "") or "",
                                "server_id": sid,
                                "source": "mcp",
                                "input_schema": getattr(t, "inputSchema", None)
                                or getattr(t, "input_schema", None)
                                or {},
                                "output_schema": getattr(t, "outputSchema", None)
                                or getattr(t, "output_schema", None)
                                or {},
                                "permission_default": "ask",
                                "owner": _tool_owner_for_catalog(tool_name),
                                "tags": _tool_tags_for_catalog(tool_name),
                                "visible_to": _tool_visible_to_for_catalog(tool_name),
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    rows.append(
                        {
                            "id": f"{sid}_error",
                            "name": f"{sid}_error",
                            "description": f"failed to list {sid} tools: {exc!r}",
                            "server_id": sid,
                            "source": "error",
                        }
                    )
        return {"tools": rows}

    @app.get("/v1/tools/{tool_id}")
    async def get_tool_detail(tool_id: str) -> dict[str, Any]:
        """SPEC §6.6 — single-tool detail. The TUI's tool-detail
        modal calls this when the user opens a row from the /tools
        catalog. Walks the same source as list_tools_unified() and
        returns the matching row, or 404 if no tool registers under
        ``tool_id``."""

        # Bundled in-process tools first — cheap.
        try:
            from clio_agent.tools.gateway import list_gateway_tools  # noqa: PLC0415

            for tool in await list_gateway_tools():
                if tool.get("name") == tool_id:
                    srv = tool.get("server", "")
                    return {
                        "id": tool_id,
                        "name": tool_id,
                        "description": tool.get("description") or "",
                        "server_id": f"mcp_{srv}" if srv else "",
                        "source": "mcp",
                        "input_schema": tool.get("input_schema") or {},
                        "output_schema": tool.get("output_schema") or {},
                        "permission_default": "ask",
                        "owner": _tool_owner_for_catalog(tool_id),
                        "tags": _tool_tags_for_catalog(tool_id),
                        "visible_to": _tool_visible_to_for_catalog(tool_id),
                    }
        except Exception:
            pass

        # Fall back to installed third-party MCP servers — heavier
        # because each lookup spawns a Client; cache could come later.
        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        if installed:
            try:
                from fastmcp import Client  # noqa: PLC0415
                from fastmcp.client.transports import (  # noqa: PLC0415
                    StdioTransport,
                    StreamableHttpTransport,
                )
            except Exception:
                Client = None  # type: ignore
            for sid, info in installed.items():
                if Client is None:
                    break
                try:
                    transport = info.get("transport") or "stdio"
                    if transport == "stdio":
                        t = StdioTransport(
                            command=info.get("command") or "",
                            args=info.get("args") or [],
                            env=info.get("env") or None,
                        )
                    else:
                        t = StreamableHttpTransport(url=info.get("url") or "")  # type: ignore[assignment]
                    async with Client(t) as cli:
                        tools = await cli.list_tools()
                    for tt in tools:
                        if getattr(tt, "name", "") == tool_id:
                            return {
                                "id": tool_id,
                                "name": tool_id,
                                "description": getattr(tt, "description", "") or "",
                                "server_id": sid,
                                "source": "mcp",
                                "input_schema": getattr(tt, "inputSchema", None)
                                or getattr(tt, "input_schema", None)
                                or {},
                                "output_schema": getattr(tt, "outputSchema", None)
                                or getattr(tt, "output_schema", None)
                                or {},
                                "permission_default": "ask",
                                "owner": _tool_owner_for_catalog(tool_id),
                                "tags": _tool_tags_for_catalog(tool_id),
                                "visible_to": _tool_visible_to_for_catalog(tool_id),
                            }
                except Exception:
                    continue

        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"tool not found: {tool_id}",
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    # ---- /v1/hooks (SPEC §6.17 declarative hooks) --------------------
    #
    # Distinct from clio_agent.runtime.hooks (in-process Python hooks
    # the framework fires on tool/message events). These are the
    # gact-tui-driven declarative hooks: id + event + (command|url) +
    # optional session_id/workspace_id scope. The TUI's `gact hook`
    # subcommand reads/writes them. In-memory; no persistence.

    @app.get("/v1/hooks")
    async def list_hooks() -> dict[str, Any]:
        return {"hooks": list(app.state.declarative_hooks.values())}

    @app.post("/v1/hooks")
    async def create_hook(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        event = (body.get("event") or "").strip()
        if not event:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="hook missing required field: event",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if not (body.get("command") or body.get("url")):
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="hook needs command or url",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        hid = body.get("id") or f"hook_{uuid.uuid4().hex[:12]}"
        row = {
            "id": hid,
            "event": event,
            "command": body.get("command") or "",
            "url": body.get("url") or "",
            "session_id": body.get("session_id") or "",
            "workspace_id": body.get("workspace_id") or "",
        }
        app.state.declarative_hooks[hid] = row
        return row

    @app.delete("/v1/hooks/{hook_id}")
    async def delete_hook(hook_id: str) -> Response:
        hook = app.state.declarative_hooks.get(hook_id)
        if hook is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"hook not found: {hook_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        _guard_direct_destructive_action(
            app,
            session_id=str(hook.get("session_id") or ""),
            workspace_id=str(hook.get("workspace_id") or ""),
            tool_name="gact.hook.delete",
            args={"hook_id": hook_id},
            summary=f"delete hook {hook_id}",
            reason="user_requested_hook_delete",
        )
        app.state.declarative_hooks.pop(hook_id, None)
        return Response(status_code=204)

    # ---- /v1/policies (SPEC §6.11.b permission policies) -------------
    #
    # Declarative allow/deny/ask rules consulted before the per-tool
    # permission_default. PUT replaces the whole list (matches the
    # gact-tui client's PutPolicies shape) and persists it locally.

    @app.get("/v1/policies")
    async def list_policies() -> dict[str, Any]:
        return {"policies": list(app.state.permission_policies)}

    @app.put("/v1/policies")
    async def put_policies(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        policies = body.get("policies")
        if not isinstance(policies, list):
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="body must be {'policies': [...]}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        clean, errors = _validate_permission_policies(policies)
        if errors:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message=("invalid permission policies; no policy changes were applied"),
                        details={
                            "policy_errors": errors,
                            "allowed_scopes": sorted(_PERMISSION_POLICY_SCOPES),
                            "allowed_actions": sorted(_PERMISSION_POLICY_ACTIONS),
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        app.state.permission_policies = clean
        _flush_permission_policies(app)
        return {"policies": clean}

    # ---- DELETE /v1/messages/{id} ------------------------------------
    #
    # gact-tui's "delete this message" gesture (used in the search
    # palette + the per-message context menu) historically hit the
    # global route. Prefer the session-scoped route so destructive
    # message deletion cannot accidentally cross session boundaries.
    # Publishes message.deleted so SSE subscribers can redraw without
    # polling.

    def _delete_message_from_session(sid: str, message_id: str) -> bool:
        msgs = app.state.messages.get(sid, [])
        for i, message in enumerate(msgs):
            if message.id != message_id:
                continue
            sess = app.state.sessions.get(sid)
            _guard_direct_destructive_action(
                app,
                session_id=sid,
                workspace_id=getattr(sess, "workspace_id", ""),
                tool_name="gact.message.delete",
                args={"message_id": message_id, "session_id": sid},
                summary=f"delete message {message_id} from session {sid}",
                reason="user_requested_message_delete",
            )
            msgs.pop(i)
            _replace_session_messages(app, sid, msgs)
            if sess is not None:
                app.state.sessions.update(sid, message_count=len(msgs))
            app.state.bus.publish(
                Event(
                    type="message.deleted",
                    session_id=sid,
                    payload={"message_id": message_id, "session_id": sid},
                )
            )
            return True
        return False

    def _message_not_found(message_id: str, *, session_id: str = "") -> HTTPException:
        details = {"message_id": message_id}
        if session_id:
            details["session_id"] = session_id
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"message not found: {message_id}",
                    details=details,
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.delete("/v1/sessions/{sid}/messages/{message_id}")
    async def delete_session_message(sid: str, message_id: str) -> Response:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if _delete_message_from_session(sid, message_id):
            return Response(status_code=204)
        raise _message_not_found(message_id, session_id=sid)

    @app.delete("/v1/messages/{message_id}")
    async def delete_message(message_id: str, session_id: str = "") -> Response:
        if session_id:
            if app.state.sessions.get(session_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="internal_error",
                            message=f"session not found: {session_id}",
                            details={"session_id": session_id},
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )
            if _delete_message_from_session(session_id, message_id):
                return Response(status_code=204)
            raise _message_not_found(message_id, session_id=session_id)
        for sid in list(app.state.messages):
            if _delete_message_from_session(sid, message_id):
                return Response(status_code=204)
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"message not found: {message_id}",
                    details={"message_id": message_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    def _make_stub(cap: str):
        # Use a Request param so FastAPI doesn't try to validate
        # path/query/body params against the handler signature —
        # stubs take anything and return 501.
        async def _stub(request: Request) -> JSONResponse:
            body = _not_implemented(cap).model_dump(exclude_none=True)
            return JSONResponse(status_code=501, content=body)

        return _stub

    for method, path, cap in _stub_routes:
        app.add_api_route(
            path,
            _make_stub(cap),
            methods=[method],
            include_in_schema=False,
        )

    def _error_code_for_status(status_code: int) -> str:
        if status_code == 404:
            return "not_found"
        if status_code == 405:
            return "unsupported"
        if status_code in {400, 422}:
            return "validation_error"
        if status_code in {401, 403}:
            return "permission_error"
        return "internal_error" if status_code >= 500 else "request_error"

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request, exc: StarletteHTTPException) -> JSONResponse:
        """Wrap HTTPExceptions in the v0.2 error envelope."""

        if isinstance(exc.detail, dict) and "error" in exc.detail:
            # Already an envelope (caller built one explicitly).
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error=_error_code_for_status(exc.status_code),
                message=str(exc.detail) if exc.detail else "",
                recoverable=exc.status_code < 500,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(exclude_none=True),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request, exc: RequestValidationError) -> JSONResponse:
        """Wrap FastAPI request validation failures in the GACT envelope."""

        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="validation_error",
                message="Request validation failed.",
                details={"errors": exc.errors()},
                recoverable=True,
            )
        )
        return JSONResponse(
            status_code=422,
            content=envelope.model_dump(exclude_none=True),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
        """Return a structured 500 for unexpected route failures."""

        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="internal_error",
                message="Unhandled server error.",
                details={
                    "original_error": type(exc).__name__,
                    "original_message": str(exc),
                },
                recoverable=False,
            )
        )
        return JSONResponse(
            status_code=500,
            content=envelope.model_dump(exclude_none=True),
        )

    return app


# Module-level ``app`` for uvicorn-style invocations:
#   uvicorn clio_agent.gact.app:app
#
# Built lazily via PEP 562 module ``__getattr__`` so that ``import
# clio_agent.gact.app`` (which the ``clio-agent-gact`` console script
# triggers) doesn't pay build_app's cost — that includes pulling in
# clio_agent.tools.execution + litellm (~4 s on Aurora's frameworks
# Python). main() constructs its own app explicitly, so the only
# consumer of this attribute is the ``uvicorn …:app`` form, which
# always materialises it on first request anyway.
_lazy_app: Optional[FastAPI] = None


def __getattr__(name: str):
    global _lazy_app  # noqa: PLW0603
    if name == "app":
        if _lazy_app is None:
            _lazy_app = build_app()
        return _lazy_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    """Console-script entry point.

    When ``CLIO_LM_PROVIDER`` is set the real ``ClioAgent`` is
    instantiated + injected so POST /messages drives a real LM.
    Otherwise the module-level ``app`` (no agent wired) runs, which
    is fine for capability introspection but 503s on /messages.
    """

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="clio-agent-gact",
        description="CLIO's GACT v0.2 REST + SSE server.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8100, type=int)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="auto-reload on source changes (dev only)",
    )
    parser.add_argument(
        "--no-agent",
        action="store_true",
        help=(
            "skip ClioAgent construction even when LM env is configured. "
            "Use when the real agent's boot cost (DSPy + ARC hydration) "
            "gets in the way of a capability-only smoke."
        ),
    )
    # gact-tui's `agent deploy` invokes adapters with --cwd; we don't
    # care about the value (CLIO reads file paths from CLIO_ALLOWED_ROOTS
    # / its own config), but the flag has to be accepted or argparse
    # bails with exit 2 and the deploy probe sees an instant zombie.
    parser.add_argument(
        "--cwd",
        default=None,
        help=(
            "ignored — accepted for compatibility with `gact agent "
            "deploy clio`, which always passes --cwd."
        ),
    )
    args = parser.parse_args()

    # Always build a fresh app inside main() — the module-level
    # ``app`` symbol is intentionally lazy (see __getattr__ above) so
    # that just importing ``clio_agent.gact.app`` doesn't pay
    # build_app's cost. When the env requests an agent we set
    # want_agent so the lifespan startup task constructs ClioAgent
    # in the background — uvicorn binds the port immediately, beating
    # gact-tui's 3-second deploy probe. POST /messages 503s until
    # app.state.agent is stamped by the background task.
    app_to_run: FastAPI = build_app()
    if not args.no_agent and os.environ.get("CLIO_LM_PROVIDER"):
        app_to_run.state.want_agent = True

    uvicorn.run(
        app_to_run,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
