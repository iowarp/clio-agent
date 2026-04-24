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
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


def _format_sse(event: "Event") -> bytes:
    """Render an Event as the SSE wire format (SPEC §7.2)::

        event: <type>
        id: <numeric monotonic id>
        data: <json envelope>
        <blank line>
    """

    payload = json.dumps(event.envelope())
    lines = (
        f"event: {event.type}\n"
        f"id: {event.id}\n"
        f"data: {payload}\n\n"
    )
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


def _iso_from_epoch(ts: float) -> str:
    """ISO-8601 UTC with microsecond precision to match the session
    registry's created_at format."""

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def _run_turn_in_background(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
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
    tools_called: list[dict[str, Any]] = []
    proposed_diffs: list[Any] = []
    nanoagents: list[Any] = []
    turn_tokens: dict[str, int] = {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
    }
    turn_cost = 0.0

    try:
        loop = asyncio.get_running_loop()
        pred = await loop.run_in_executor(
            None,
            lambda: app.state.agent.forward(user_text, session_id=sid),
        )
        answer_text = getattr(pred, "answer", "")
        selected_agent = getattr(pred, "selected_expert", "") or ""
        rationale = getattr(pred, "routing_rationale", "")
        tools_called = _extract_tools_called(pred)
        # CLIO-BBBBBBBBBB24: cost + token rollup. Real DSPy
        # predictions don't always populate .tokens / .cost_usd
        # directly — pull from dspy.LM history when the prediction
        # itself doesn't carry them.
        raw_tokens = getattr(pred, "tokens", None)
        if raw_tokens is not None:
            for key in turn_tokens:
                if isinstance(raw_tokens, dict):
                    v = raw_tokens.get(key, 0)
                else:
                    v = getattr(raw_tokens, key, 0)
                turn_tokens[key] = int(v or 0)
        else:
            usage = _usage_from_dspy_history()
            for key in turn_tokens:
                turn_tokens[key] = int(usage.get(key, 0) or 0)
            turn_cost = float(usage.get("cost_usd", 0.0) or 0.0)
        if not turn_cost:
            turn_cost = float(getattr(pred, "cost_usd", 0.0) or 0.0)
        proposed_diffs = list(getattr(pred, "file_diffs", None) or [])
        nanoagents = list(getattr(pred, "nanoagents_spawned", None) or [])
        for req in (getattr(pred, "permissions_requested", None) or []):
            src = req if isinstance(req, dict) else {
                "tool_call": getattr(req, "tool_call", {}),
                "summary": getattr(req, "summary", ""),
                "id": getattr(req, "id", ""),
            }
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
            bus.publish(Event(
                type="permission.requested",
                session_id=sid,
                payload=row,
            ))
        if sid in app.state.cancel_flags:
            app.state.cancel_flags.discard(sid)
            error_info = ErrorInfo(
                error="cancelled",
                message="turn cancelled by client",
                details={"session_id": sid},
                recoverable=True,
            )
            answer_text = ""
            tools_called = []
    except asyncio.CancelledError:
        error_info = ErrorInfo(
            error="cancelled",
            message="turn cancelled by client",
            details={"session_id": sid},
            recoverable=True,
        )
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
    # got a selected_agent, then the text answer, then any file_diffs.
    assistant_parts: list[Part] = []
    if selected_agent:
        assistant_parts.append(Part(
            id=_new_part_id(),
            type="routing_decision",
            selected_agent=selected_agent,
            rationale=rationale,
            confidence=0.0,
            heuristic=False,
        ))
    if answer_text:
        assistant_parts.append(
            Part(id=_new_part_id(), type="text", text=answer_text)
        )
    for row in proposed_diffs:
        if isinstance(row, dict):
            path = row.get("path", "")
            udiff = row.get("unified_diff", "")
        else:
            path = getattr(row, "path", "")
            udiff = getattr(row, "unified_diff", "")
        if not path or not udiff:
            continue
        assistant_parts.append(Part(
            id=_new_part_id(),
            type="file_diff",
            path=path,
            unified_diff=udiff,
            status="pending",
        ))

    assistant_metadata: dict[str, Any] = {}
    if tools_called:
        assistant_metadata["tools_called"] = tools_called
    assistant_msg = Message(
        id=_new_message_id("asst"),
        session_id=sid,
        role="assistant",
        created_at=_iso_from_epoch(time.time()),
        updated_at=_iso_from_epoch(time.time()),
        parts=assistant_parts,
        tokens=Tokens(**turn_tokens),
        cost_usd=turn_cost,
        stop_reason="error" if error_info else "end_turn",
        error_info=error_info,
        metadata=assistant_metadata,
    )

    # Index file_diff parts so /diffs/apply + /diffs/reject find them.
    bucket = app.state.pending_diffs.setdefault(sid, [])
    for p in assistant_parts:
        if p.type != "file_diff":
            continue
        bucket.append({
            "path": p.path,
            "unified_diff": p.unified_diff,
            "status": "pending",
            "part_id": p.id,
            "message_id": assistant_msg.id,
        })

    # Materialise nanoagent spawns + publish their lifecycle events.
    for spawn in nanoagents:
        get = spawn.get if isinstance(spawn, dict) else (
            lambda k, default=None, _s=spawn: getattr(_s, k, default)
        )
        agent_id = get("agent_id") or get("agent") or "nanoagent"
        spawn_input = get("input") or {}
        answer = get("answer") or ""
        subsess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=f"{agent_id} subagent",
            parent_session_id=sid,
        )
        sub_now = time.time()
        sub_user = Message(
            id=_new_message_id("user"),
            session_id=subsess.id,
            role="user",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[Part(
                id=_new_part_id(), type="text", text=str(spawn_input),
            )],
        )
        sub_asst = Message(
            id=_new_message_id("asst"),
            session_id=subsess.id,
            role="assistant",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[Part(id=_new_part_id(), type="text", text=answer)] if answer else [],
            stop_reason="end_turn",
        )
        app.state.messages.setdefault(subsess.id, []).extend(
            [sub_user, sub_asst]
        )
        app.state.sessions.update(
            subsess.id, message_count=2, status="idle"
        )
        bus.publish(Event(
            type="subagent.started",
            session_id=sid,
            payload={
                "parent_session_id": sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "spawned_by_message_id": assistant_msg.id,
            },
        ))
        bus.publish(Event(
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
        ))

    # message.created for the assistant message (empty body — parts
    # arrive via subsequent message.part.added/delta events).
    bus.publish(Event(
        type="message.created", session_id=sid,
        payload=Message(
            id=assistant_msg.id,
            session_id=sid,
            role="assistant",
            created_at=assistant_msg.created_at,
            updated_at=assistant_msg.updated_at,
            parts=[],
        ).model_dump(exclude_none=True),
    ))
    # Stream text parts via message.part.added (empty) + N
    # message.part.delta + message.part.completed.
    _CHUNK = 64
    for part in assistant_parts:
        if part.type == "text" and part.text:
            stub = part.model_copy(deep=True)
            stub.text = ""
            bus.publish(Event(
                type="message.part.added",
                session_id=sid,
                payload={
                    "message_id": assistant_msg.id,
                    "part": stub.model_dump(exclude_none=True),
                },
            ))
            full = part.text
            for i in range(0, len(full), _CHUNK):
                bus.publish(Event(
                    type="message.part.delta",
                    session_id=sid,
                    payload={
                        "message_id": assistant_msg.id,
                        "part_id": part.id,
                        "delta": {"text_append": full[i:i + _CHUNK]},
                    },
                ))
            bus.publish(Event(
                type="message.part.completed",
                session_id=sid,
                payload={
                    "message_id": assistant_msg.id,
                    "part_id": part.id,
                },
            ))
        else:
            bus.publish(Event(
                type="message.part.added",
                session_id=sid,
                payload={
                    "message_id": assistant_msg.id,
                    "part": part.model_dump(exclude_none=True),
                },
            ))
    # Per-tool telemetry events synthesised from the prediction's
    # tool_called trace. A real ReAct agent that instruments
    # MCPToolBridge.call_tool publishes the same wire shape live;
    # the TUI's renderer doesn't care which flavour it is.
    for idx, call in enumerate(tools_called):
        call_id = f"call_{assistant_msg.id}_{idx}"
        bus.publish(Event(
            type="tool.call.started",
            session_id=sid,
            payload={
                "message_id": assistant_msg.id,
                "call_id": call_id,
                "tool": call.get("name", ""),
                "args": call.get("args", {}),
            },
        ))
        bus.publish(Event(
            type="tool.call.completed",
            session_id=sid,
            payload={
                "message_id": assistant_msg.id,
                "call_id": call_id,
                "tool": call.get("name", ""),
                "ok": call.get("ok", True),
                "duration_ms": call.get("duration_ms", 0.0),
                "cached": call.get("cached", False),
            },
        ))
    completed_payload: dict[str, Any] = {
        "message_id": assistant_msg.id,
        "stop_reason": "error" if error_info else "end_turn",
        "tokens": dict(turn_tokens),
        "cost_usd": turn_cost,
    }
    if tools_called:
        completed_payload["metadata"] = {"tools_called": tools_called}
    bus.publish(Event(
        type="message.completed",
        session_id=sid,
        payload=completed_payload,
    ))

    # Persist + settle.
    app.state.messages.setdefault(sid, []).append(assistant_msg)
    app.state.sessions.update(
        sid,
        status="idle" if error_info is None else "error",
        message_count=sess.message_count + 2,
        add_tokens_input=turn_tokens["input"],
        add_tokens_output=turn_tokens["output"],
        add_cost_usd=turn_cost,
    )
    bus.publish(Event(
        type="session.status_changed",
        session_id=sid,
        payload={
            "session_id": sid,
            "status": "error" if error_info else "idle",
            "prev_status": "running",
        },
    ))


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
    usage = (
        last.get("usage")
        if isinstance(last, dict)
        else getattr(last, "usage", None)
    )
    if usage is None and isinstance(last, dict):
        resp = last.get("response", {}) or {}
        usage = resp.get("usage", {}) if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {
        "input": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        "cost_usd": float(usage.get("cost_usd") or 0.0),
    }


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
        if isinstance(call, dict):
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return _src.get(key, default)
        else:
            # msgspec structs + DSPy trace records — attribute access.
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return getattr(_src, key, default)

        name = get("name") or get("tool") or ""
        if name:
            row["name"] = str(name)

        args = get("args")
        if args is None:
            args = get("arguments")
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
    "analysis": "data_analysis",
    "visualization": "data_visualization",
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
    ],
    "analysis": [
        "parquet_analyze_schema",
        "parquet_query_data",
        "parquet_compute_statistics",
    ],
    "visualization": [
        "plot_histogram",
        "plot_bar_chart",
        "plot_scatter",
        "plot_summary",
    ],
}


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

    from clio_agent.experts import get_expert_capabilities

    rows: list[AgentDef] = [
        AgentDef(
            id="main",
            source="builtin",
            title="Main Agent",
            description=(
                "Tier-1 orchestrator. Routes user queries to tier-2 "
                "specialists based on keyword heuristics + LM classifier."
            ),
            tier=1,
            specialization="orchestrator",
        ),
    ]

    for expert_id, caps in get_expert_capabilities().items():
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
                tools=tools,
                tier=2,
                specialization=_EXPERT_SPECIALIZATION.get(
                    expert_id, expert_id
                ),
                keywords=keywords,
            )
        )

    return rows


def _builtin_tools() -> list[Tool]:
    """Flatten the experts' curated tool lists into a single GACT
    Tool catalog. Stable ids (same strings the experts reference),
    backend flag `builtin`. The names MAY duplicate across experts
    (e.g. read_file) — we dedupe by id so GET /v1/catalog/tools has
    one row per distinct tool."""

    seen: dict[str, Tool] = {}
    for agent in _builtin_agents():
        if agent.tier != 2:
            continue
        for tool_name in agent.tools:
            if tool_name in seen:
                continue
            seen[tool_name] = Tool(
                id=tool_name,
                source="builtin",
                name=tool_name,
                title=tool_name.replace("_", " ").title(),
            )
    return list(seen.values())

from typing import Any, Protocol

from clio_agent.gact.events import Event, EventBus, heartbeat_payload
from clio_agent.gact.sessions import SessionStore, _default_store_path
from clio_agent.gact.types import (
    AgentDef,
    AuthInfo,
    BackendInfo,
    CacheStats,
    Capabilities,
    CapabilityFlags,
    CreateSessionRequest,
    ErrorEnvelope,
    ErrorInfo,
    GlobalMemoryStats,
    HealthResponse,
    Integration,
    ListAgentsResponse,
    ListSessionsResponse,
    ListToolsResponse,
    MemoryStats,
    Message,
    Metrics,
    MetricsMessages,
    MetricsSessions,
    Part,
    PostMessageRequest,
    PostMessageResponse,
    Session,
    SessionMemoryStats,
    Tokens,
    Tool,
    TransportFlags,
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

    Today: records the boot timestamp so ``/v1/health`` can report
    uptime. Future: wire ClioAgent, mount MCP gateway, load config,
    etc. (CLIO-BBBBBBBBBB7+).
    """

    app.state.started_at = time.time()
    yield
    # No-op shutdown for now; ClioAgent.shutdown goes here once
    # wired.


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
    app.state.sessions = SessionStore(
        path=sessions_path if sessions_path is not None else _default_store_path()
    )
    app.state.agent = agent  # may be None; POST message checks before using
    app.state.arc = arc  # may be None; /v1/memory/stats returns zeros in that case
    # CLIO-BBBBBBBBBB13: per-session pub/sub. POST /messages
    # publishes; /v1/sessions/{sid}/events subscribers consume.
    app.state.bus = EventBus()
    # CLIO-BBBBBBBBBB14: in-memory message log keyed by session_id.
    # Populated by POST /messages, read by GET /messages. Not
    # persisted across restarts — disk-backed persistence lives in
    # the CLIO catch-up phase alongside ARC session replay.
    app.state.messages: dict[str, list[Message]] = {}
    # CLIO-BBBBBBBBBB20: cooperative cancellation flags. POST /cancel
    # adds a sid; the POST-message handler checks + clears after the
    # agent returns. Set (not dict) because the flag's presence IS
    # the signal — no payload.
    app.state.cancel_flags: set[str] = set()
    # CLIO-BBBBBBBBBB22: per-session context files. Keyed by
    # session_id, each value is an ordered dict of
    # path -> ContextFile dict.
    app.state.context_files: dict[str, dict[str, dict[str, Any]]] = {}
    # CLIO-BBBBBBBBBB21: per-session pending diffs. Keyed by
    # session_id -> list of {path, unified_diff, status,
    # part_id, message_id}. Status is "pending" until apply/reject
    # flips it.
    app.state.pending_diffs: dict[str, list[dict[str, Any]]] = {}
    # CLIO-BBBBBBBBBB23: pending permission requests. Flat dict
    # keyed by permission_id so GET /v1/permissions can filter by
    # session cheaply. Each record carries
    # {id, session_id, tool_call, summary, created_at, status,
    #  action, resolved_at}.
    app.state.permissions: dict[str, dict[str, Any]] = {}

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
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
            rows.append(Integration(
                name="agent",
                status="unavailable",
                detail="no ClioAgent wired; POST /messages will 503",
            ))
        else:
            # Heuristic: the production ClioAgent is a class that
            # imports DSPy under the hood and exposes it via
            # `agent.__class__.__module__`. The smoke/test fakes
            # live under 'gact_smoke_server' or '__main__'. Label
            # them so the /doctor modal is honest about what's
            # running.
            mod = type(agent).__module__
            is_fake = (
                "smoke" in mod
                or mod == "__main__"
                or "test" in mod.lower()
            )
            rows.append(Integration(
                name="agent",
                status="degraded" if is_fake else "ready",
                detail=(
                    f"{type(agent).__name__} (fake — dev harness)"
                    if is_fake
                    else f"{type(agent).__name__} wired"
                ),
            ))

        if app.state.arc is None:
            rows.append(Integration(
                name="arc",
                status="degraded",
                detail="no ARC wired; /v1/memory/stats returns zeros",
            ))
        else:
            try:
                stats = app.state.arc.get_cache_stats()
                hr = stats.get("hit_rate", 0.0)
                rows.append(Integration(
                    name="arc",
                    status="ready",
                    detail=f"cache {int(hr * 100)}% hit rate",
                ))
            except Exception as exc:
                rows.append(Integration(
                    name="arc",
                    status="unavailable",
                    detail=f"ARC.get_cache_stats raised: {exc!r}",
                ))

        # Worst-status wins.
        statuses = {r.status for r in rows}
        if "unavailable" in statuses:
            overall = "unavailable"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "ready"

        return HealthResponse(
            healthy=overall != "unavailable",
            uptime_s=uptime,
            overall_status=overall,
            integrations=rows,
        )

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
                commands=False,
                metrics=True,  # BBB15 — /v1/metrics returns SPEC §6.16 envelope
                session_branching=True,  # BBB26 — POST /sessions/{sid}/fork
                search_messages=True,  # BBB27 — GET /sessions/{sid}/messages/search
                cost_tracking=True,  # BBB24 — Message.tokens + Session.cost_usd rollup
                files=True,  # BBB22 — /v1/sessions/{sid}/context/files CRUD
                diffs=True,  # BBB21 — file_diff parts + /diffs/apply,reject
                permissions=True,  # BBB23 — /v1/permissions + permission.* events
                subagents=True,  # BBB25 — nanoagent subsessions + subagent.* events
                # v0.2 additions — advertised when the scaffold
                # actually emits them. Turned on piecewise as the
                # follow-on items land.
                agent_routing=True,  # BBB10 — /v1/agents?tier= + tier-2 catalog
                memory=True,  # BBB11 — /v1/memory/stats backed by ARC
                structured_errors=True,  # always — we return the envelope for every error
                integration_health=True,  # /v1/health above carries it
                tool_telemetry=True,  # BBB18 — tool.call.started/completed events
            ),
            transports=TransportFlags(events_sse=True, events_websocket=False),
            auth=AuthInfo(schemes=["trust_socket"], current="trust_socket"),
        )

    # ---- 501 stubs for the rest of the surface ---------------------------
    # Every route in the v0.2 contract that we haven't wired yet
    # returns the structured error envelope from above. Matches the
    # shape v0.2 clients expect, while honestly reporting that the
    # backend doesn't yet implement the endpoint.

    # ---- /v1/sessions CRUD -----------------------------------------
    # CLIO-BBBBBBBBBB8 — four real handlers against app.state.sessions
    # (the SessionStore wired above). Kept as nested closures so they
    # can close over `app` cleanly without passing the store around.

    @app.post("/v1/sessions", response_model=Session)
    async def create_session(req: CreateSessionRequest) -> Session:
        sess = app.state.sessions.create(
            workspace_id=req.workspace_id or "ws_default",
            title=req.title,
            metadata=req.metadata,
        )
        return Session(**sess.to_wire())

    @app.get("/v1/sessions", response_model=ListSessionsResponse)
    async def list_sessions(workspace_id: Optional[str] = None) -> ListSessionsResponse:
        rows = app.state.sessions.list(workspace_id=workspace_id)
        return ListSessionsResponse(
            sessions=[Session(**row.to_wire()) for row in rows]
        )

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

    @app.delete("/v1/sessions/{sid}")
    async def delete_session(sid: str) -> JSONResponse:
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
        return JSONResponse(status_code=204, content=None)

    # ---- /v1/permissions (BBB23) --------------------------------------

    @app.get("/v1/permissions")
    async def list_permissions(
        session_id: str = "", status: str = ""
    ) -> dict[str, Any]:
        """List permission requests.

        ?session_id=<sid> narrows to a session; ?status=pending
        hides resolved rows. Both are optional.
        """

        rows = list(app.state.permissions.values())
        if session_id:
            rows = [r for r in rows if r.get("session_id") == session_id]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return {"permissions": rows}

    @app.post("/v1/permissions/{pid}")
    async def respond_permission(
        pid: str, request: Request
    ) -> JSONResponse:
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
        if action not in {
            "allow", "deny", "allow_session", "allow_workspace"
        }:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(
                            "action must be one of allow, deny, "
                            "allow_session, allow_workspace"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if row.get("status") == "pending":
            row["status"] = "resolved"
            row["action"] = action
            row["resolved_at"] = datetime.now(timezone.utc).isoformat()
            app.state.bus.publish(Event(
                type="permission.resolved",
                session_id=row.get("session_id", ""),
                payload={
                    "permission_id": pid,
                    "action": action,
                    "session_id": row.get("session_id", ""),
                },
            ))
        return JSONResponse(status_code=204, content=None)

    # ---- /v1/sessions/{sid}/diffs/* (BBB21) ---------------------------

    def _filter_diff_paths(
        rows: list[dict[str, Any]], paths: list[str]
    ) -> list[dict[str, Any]]:
        """Narrow pending diffs to a given path allow-list. Empty
        list (or no param) means "every pending row"."""

        if not paths:
            return [r for r in rows if r["status"] == "pending"]
        allow = set(paths)
        return [r for r in rows if r["path"] in allow and r["status"] == "pending"]

    @app.post("/v1/sessions/{sid}/diffs/apply")
    async def diffs_apply(
        sid: str, request: Request
    ) -> dict[str, list[str]]:
        """Mark pending diffs as applied + publish events.

        Body: ``{paths: [...]}`` (optional). If omitted, every
        pending diff is applied. Returns ``{applied: [...]}``. Does
        NOT actually touch the filesystem — the agent is responsible
        for that once it sees the ``file.diff.applied`` event; this
        endpoint just records the user's decision and broadcasts it.
        """

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
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]

        rows = app.state.pending_diffs.get(sid, [])
        targets = _filter_diff_paths(rows, paths)
        applied: list[str] = []
        for r in targets:
            r["status"] = "applied"
            applied.append(r["path"])
            app.state.bus.publish(Event(
                type="file.diff.applied",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "path": r["path"],
                    "part_id": r.get("part_id", ""),
                    "message_id": r.get("message_id", ""),
                },
            ))
        return {"applied": applied}

    @app.post("/v1/sessions/{sid}/diffs/reject")
    async def diffs_reject(
        sid: str, request: Request
    ) -> dict[str, list[str]]:
        """Mark pending diffs as rejected + publish events."""

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
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]

        rows = app.state.pending_diffs.get(sid, [])
        targets = _filter_diff_paths(rows, paths)
        rejected: list[str] = []
        for r in targets:
            r["status"] = "rejected"
            rejected.append(r["path"])
            app.state.bus.publish(Event(
                type="file.diff.rejected",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "path": r["path"],
                    "part_id": r.get("part_id", ""),
                    "message_id": r.get("message_id", ""),
                },
            ))
        return {"rejected": rejected}

    # ---- /v1/sessions/{sid}/context/files (BBB22) ---------------------

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
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = (body.get("path") or "").strip()
        if not path:
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
        mode = body.get("mode") or "read"
        if mode not in {"edit", "read", "pin"}:
            mode = "read"
        row = {
            "path": path,
            "mode": mode,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "last_modified": body.get("last_modified") or "",
            "size": int(body.get("size") or 0),
            "language": body.get("language") or "",
        }
        bucket = app.state.context_files.setdefault(sid, {})
        bucket[path] = row
        app.state.bus.publish(Event(
            type="context.file.added",
            session_id=sid,
            payload={"session_id": sid, "file": row},
        ))
        return row

    @app.delete("/v1/sessions/{sid}/context/files")
    async def remove_context_file(
        sid: str, request: Request
    ) -> JSONResponse:
        """Detach a file by path. 204 whether the path was attached
        — the TUI fires this optimistically on `d` in the context
        pane and doesn't want to error if the file was already
        removed."""

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
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = (body.get("path") or "").strip()
        bucket = app.state.context_files.get(sid, {})
        removed = bucket.pop(path, None) if path else None
        if removed is not None:
            app.state.bus.publish(Event(
                type="context.file.removed",
                session_id=sid,
                payload={"session_id": sid, "path": path},
            ))
        return JSONResponse(status_code=204, content=None)

    # ---- POST /v1/sessions/{sid}/fork (BBB26) -------------------------

    @app.post("/v1/sessions/{sid}/fork")
    async def fork_session(sid: str, request: Request) -> JSONResponse:
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
        app.state.messages[new_sess.id] = [m.model_copy(deep=True) for m in src_msgs]
        app.state.sessions.update(
            new_sess.id, message_count=len(src_msgs)
        )
        return JSONResponse(
            status_code=201,
            content=Session(**new_sess.to_wire()).model_dump(exclude_none=True),
        )

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
                matches.append({
                    "message_id": m.id,
                    "part_id": part.id,
                    "snippet": snippet,
                    "score": 1.0 + (idx * 0.01),
                })
        matches.sort(key=lambda r: r["score"], reverse=True)
        return {"matches": matches}

    # ---- POST /v1/sessions/{sid}/cancel (BBB20) -----------------------

    @app.post("/v1/sessions/{sid}/cancel")
    async def cancel_session(sid: str) -> JSONResponse:
        """Cooperative cancel of an in-flight turn on this session.

        The agent's ``forward()`` checks ``agent.is_cancelled(sid)``
        periodically (or honors a threading.Event we hand it) and
        returns early with ``error_info.error == "cancelled"``. The
        endpoint itself just flips the flag + publishes a
        ``session.cancelled`` event so any live SSE subscriber sees
        the transition without waiting for the next turn boundary.

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

        # Set the cancellation flag. The POST-message handler checks
        # this after forward() returns so even agents that don't
        # cooperate produce a cancelled-looking turn envelope.
        app.state.cancel_flags.add(sid)
        # Cooperative-only today: the background turn task checks
        # cancel_flags after forward() returns so the assistant
        # message reports error="cancelled" rather than its real
        # output. True hard-abort during a long forward() is a
        # follow-up — needs an asyncio.create_task we can grab
        # back, which BackgroundTasks doesn't expose.
        app.state.sessions.update(sid, status="cancelled")
        app.state.bus.publish(Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": "cancelled",
                "prev_status": sess.status,
            },
        ))
        return JSONResponse(status_code=204, content=None)

    # ---- POST /v1/sessions/{sid}/messages (BBB9) ---------------------
    # Non-streaming turn: 1 request, 1 response body containing both
    # the stored user message + the assistant's reply. Streaming
    # (SSE on /v1/sessions/{sid}/events) lands in BBB10.

    @app.post(
        "/v1/sessions/{sid}/messages", response_model=PostMessageResponse
    )
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
        if app.state.agent is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="config_error",
                        message=(
                            "ClioAgent not wired into this build. Launch "
                            "`clio-agent-gact` with CLIO_LM_PROVIDER set "
                            "or pass `agent=...` to build_app()."
                        ),
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        user_text = req.extract_text()
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

        now = time.time()
        user_msg = Message(
            id=_new_message_id("user"),
            session_id=sid,
            role="user",
            created_at=_iso_from_epoch(now),
            updated_at=_iso_from_epoch(now),
            parts=[Part(id=_new_part_id(), type="text", text=user_text)],
            metadata=req.metadata,
        )

        # Persist + publish the user message synchronously so by the
        # time the ack returns, GET /messages reflects it. Then mark
        # the session running, then schedule the turn in the
        # background and return.
        app.state.messages.setdefault(sid, []).append(user_msg)
        app.state.sessions.update(sid, status="running")
        app.state.bus.publish(Event(
            type="session.status_changed",
            session_id=sid,
            payload={"session_id": sid, "status": "running", "prev_status": "idle"},
        ))
        app.state.bus.publish(Event(
            type="message.created",
            session_id=sid,
            payload=user_msg.model_dump(exclude_none=True),
        ))

        # FastAPI runs background_tasks after the response is sent.
        # That gives us the "ack-and-stream" semantics the TUI wants
        # — POST returns in milliseconds, SSE delivers progress as
        # the agent ticks, /cancel can interrupt mid-flight.
        background_tasks.add_task(
            _run_turn_in_background, app, sid, user_text, user_msg
        )

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

    # ---- /v1/agents catalog (BBB10) ----------------------------------

    @app.get("/v1/agents", response_model=ListAgentsResponse)
    async def list_agents(tier: Optional[int] = None) -> ListAgentsResponse:
        """SPEC §6.5 + v0.2 §4.3.1: optional ?tier=N filter."""
        rows = _builtin_agents()
        if tier is not None:
            rows = [a for a in rows if a.tier == tier]
        return ListAgentsResponse(agents=rows)

    @app.get("/v1/catalog/tools", response_model=ListToolsResponse)
    async def list_tools() -> ListToolsResponse:
        return ListToolsResponse(tools=_builtin_tools())

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
        if session_id:
            sess_rec = app.state.sessions.get(session_id)
            if sess_rec is not None:
                # CLIO tracks tokens per invocation, not per
                # session; for the TUI's purposes message_count is
                # a reasonable proxy until BBB19 moves sessions into
                # ARC and per-turn tokens become available on the
                # Session record.
                session_block = SessionMemoryStats(
                    session_id=session_id,
                    messages_retained=sess_rec.message_count,
                    tokens_retained=0,
                    tokens_budget=4000,
                    profiles_attached=0,
                )
            else:
                # Unknown session: return an empty block rather than
                # a 404. The TUI's footer chip handles zero stats
                # gracefully; a 404 would spam the logs on every
                # mis-timed fetch.
                session_block = SessionMemoryStats(session_id=session_id)

        return MemoryStats(
            cache=cache,
            session=session_block,
            global_=global_stats,
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
                last_event_id = int(
                    request.headers.get("last-event-id", "0")
                )
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

    # ---- 501 stubs for the still-unwired v0.2 surface ----------------

    _stub_routes: list[tuple[str, str, str]] = [
        # (method, path, capability_name_for_error)
        ("GET", "/v1/workspaces", "workspaces"),
        ("GET", "/v1/tools", "tools"),
        ("GET", "/v1/commands", "commands"),
    ]

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

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request, exc: HTTPException
    ) -> JSONResponse:
        """Wrap HTTPExceptions in the v0.2 error envelope."""

        if isinstance(exc.detail, dict) and "error" in exc.detail:
            # Already an envelope (caller built one explicitly).
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="internal_error",
                message=str(exc.detail) if exc.detail else "",
                recoverable=exc.status_code < 500,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(exclude_none=True),
        )

    return app


# Module-level app for uvicorn-style invocations:
#   uvicorn clio_agent.gact.app:app
app = build_app()


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
    args = parser.parse_args()

    # CLIO-BBBBBBBBBB-D2: auto-wire the real ClioAgent when the env
    # gives us an LM endpoint. Falls back to the no-agent module-
    # level app on import / construction failures — a bootable GACT
    # surface is strictly better than a stack trace.
    app_to_run: FastAPI = app
    if not args.no_agent and os.environ.get("CLIO_LM_PROVIDER"):
        try:
            import dspy

            from clio_agent.agent import ClioAgent
            from clio_agent.config import create_lm, load_config_from_env

            provider_cfg = load_config_from_env()
            dspy.configure(lm=create_lm(provider_cfg))
            agent = ClioAgent(verbose=False)
            app_to_run = build_app(agent=agent, arc=agent.arc)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[clio-agent-gact] ClioAgent init failed ({exc!r}); "
                "running with no agent wired. POST /messages will 503.",
                flush=True,
            )

    uvicorn.run(
        app_to_run,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
