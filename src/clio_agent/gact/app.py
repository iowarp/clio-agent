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
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
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
                # v0.2 additions — advertised when the scaffold
                # actually emits them. Turned on piecewise as the
                # follow-on items land.
                agent_routing=True,  # BBB10 — /v1/agents?tier= + tier-2 catalog
                memory=True,  # BBB11 — /v1/memory/stats backed by ARC
                structured_errors=True,  # always — we return the envelope for every error
                integration_health=True,  # /v1/health above carries it
                tool_telemetry=False,
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
        sid: str, req: PostMessageRequest
    ) -> PostMessageResponse:
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
                            "ClioAgent not wired into this build. Launch via "
                            "`clio-agent-gact` (which constructs a real agent) "
                            "or pass `agent=...` to build_app()."
                        ),
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        # Mark the session running + stamp updated_at so clients see
        # the transition even though no SSE stream is live for the
        # non-streaming path.
        app.state.sessions.update(sid, status="running")
        # CLIO-BBBBBBBBBB13: publish so any open SSE subscriber sees
        # the same lifecycle the non-streaming response will report.
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={"session_id": sid, "status": "running", "prev_status": "idle"},
            )
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

        error_info: Optional[ErrorInfo] = None
        answer_text = ""
        selected_agent = ""
        rationale = ""
        tools_called: list[dict[str, Any]] = []
        turn_tokens: dict[str, int] = {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        }
        turn_cost = 0.0

        try:
            pred = app.state.agent.forward(user_text, session_id=sid)
            answer_text = getattr(pred, "answer", "")
            selected_agent = getattr(pred, "selected_expert", "") or ""
            rationale = getattr(pred, "routing_rationale", "")
            # Optional: the agent may expose ToolCall traces from the
            # underlying ReAct loop. Normalise whatever shape the
            # agent emits (ARC ToolCall struct, DSPy trace row, plain
            # dict) into the v0.2 wire shape `{name, args, ok,
            # duration_ms, cached}` — all fields optional so the TUI
            # renders whatever's present.
            tools_called = _extract_tools_called(pred)
            # CLIO-BBBBBBBBBB24: cost + token rollup. Agent may hang
            # `.tokens` (dict or attr-accessible) and `.cost_usd` on
            # its Prediction; both default to zero if absent.
            raw_tokens = getattr(pred, "tokens", None)
            if raw_tokens is not None:
                for key in turn_tokens:
                    if isinstance(raw_tokens, dict):
                        v = raw_tokens.get(key, 0)
                    else:
                        v = getattr(raw_tokens, key, 0)
                    turn_tokens[key] = int(v or 0)
            turn_cost = float(getattr(pred, "cost_usd", 0.0) or 0.0)
            # CLIO-BBBBBBBBBB20: if /cancel arrived while the agent
            # was mid-forward, override the turn as a cancellation.
            # Agents that cooperate (check an is_cancelled hook) get
            # here with partial output we discard; agents that don't
            # (including FakeClioAgent) have their answer_text
            # thrown away and the turn still reports cancelled —
            # consistent UX either way.
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
        except Exception as exc:
            error_info = ErrorInfo(
                error="agent_error",
                message=f"agent.forward raised: {exc}",
                details={"original_error": type(exc).__name__},
                recoverable=True,
            )
            app.state.sessions.update(sid, status="error")

        # Build assistant parts — routing_decision (v0.2) first when
        # we got a selected_agent, then the text answer.
        assistant_parts: list[Part] = []
        if selected_agent:
            assistant_parts.append(
                Part(
                    id=_new_part_id(),
                    type="routing_decision",
                    selected_agent=selected_agent,
                    rationale=rationale,
                    confidence=0.0,  # unknown at this layer
                    heuristic=False,
                )
            )
        if answer_text:
            assistant_parts.append(
                Part(id=_new_part_id(), type="text", text=answer_text)
            )

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

        # Settle the session back to idle (or error, if we already
        # stamped it above). Also accumulate the turn's cost/tokens
        # onto the session rollup so /v1/sessions and the TUI
        # sidebar have a running total.
        app.state.sessions.update(
            sid,
            status="idle" if error_info is None else None,
            message_count=sess.message_count + 2,
            add_tokens_input=turn_tokens["input"],
            add_tokens_output=turn_tokens["output"],
            add_cost_usd=turn_cost,
        )

        # CLIO-BBBBBBBBBB13: publish per-message events so live
        # SSE subscribers see the turn unfold. Order mirrors
        # SPEC §7.4: the user message arrives first, then the
        # assistant message body grows part-by-part, then completion.
        # Payload shape: the Message object directly, not wrapped
        # under a `{"message": ...}` key. Matches what the reference
        # emulator emits + what the TUI's decodeMessage expects
        # (the inner payload IS the Message).
        bus: EventBus = app.state.bus
        bus.publish(Event(
            type="message.created", session_id=sid,
            payload=user_msg.model_dump(exclude_none=True),
        ))
        bus.publish(Event(
            type="message.created", session_id=sid,
            payload=Message(
                id=assistant_msg.id,
                session_id=sid,
                role="assistant",
                created_at=assistant_msg.created_at,
                updated_at=assistant_msg.updated_at,
                parts=[],  # parts arrive via subsequent .added events
            ).model_dump(exclude_none=True),
        ))
        for part in assistant_parts:
            bus.publish(Event(
                type="message.part.added",
                session_id=sid,
                payload={
                    "message_id": assistant_msg.id,
                    "part": part.model_dump(exclude_none=True),
                },
            ))
        completed_payload: dict[str, Any] = {
            "message_id": assistant_msg.id,
            "stop_reason": "error" if error_info else "end_turn",
            "tokens": dict(turn_tokens),
            "cost_usd": turn_cost,
        }
        if tools_called:
            # BBB16: the TUI renders a post-hoc gutter under the turn
            # by reading metadata.tools_called off the completion
            # event OR the assistant message. We emit both for
            # redundancy — the emulator only populates it on the
            # completion event, but downstream consumers of the
            # persisted message log want it on the message too.
            completed_payload["metadata"] = {"tools_called": tools_called}
        bus.publish(Event(
            type="message.completed",
            session_id=sid,
            payload=completed_payload,
        ))
        bus.publish(Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": "error" if error_info else "idle",
                "prev_status": "running",
            },
        ))

        log = app.state.messages.setdefault(sid, [])
        log.append(user_msg)
        log.append(assistant_msg)

        return PostMessageResponse(
            user_message=user_msg, assistant_message=assistant_msg
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

    Keeps its flag surface narrow on purpose — uvicorn has plenty of
    knobs but most operators just want ``--host`` and ``--port``.
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
    args = parser.parse_args()
    uvicorn.run(
        "clio_agent.gact.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
