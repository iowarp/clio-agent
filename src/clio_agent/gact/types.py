"""GACT v0.2 wire types as Pydantic models.

Shapes mirror ``gact-tui/contract/SPEC.md`` (v0.2). This module is
the Python counterpart of the Go types at
``gact-tui/emulator/pkg/gact/``; field names + JSON keys match so
generated client code rounds-trips cleanly.

Only the shapes we actually emit or consume live here — we stub
incrementally as each endpoint lands (CLIO-BBBBBBBBBB6 onwards).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# §3 — health + capabilities
# ---------------------------------------------------------------------------


class Integration(BaseModel):
    """One subsystem row in ``/v1/health.integrations[]`` (v0.2 §3.4)."""

    name: str
    status: Literal["ready", "degraded", "unavailable"]
    detail: str = ""


class HealthResponse(BaseModel):
    """GET /v1/health — SPEC §3.4 with v0.2 integration_health additions."""

    healthy: bool
    uptime_s: int
    overall_status: Optional[Literal["ready", "degraded", "unavailable"]] = None
    integrations: Optional[list[Integration]] = None


class BackendInfo(BaseModel):
    name: str
    version: str
    vendor: str
    homepage: str = ""


class CapabilityFlags(BaseModel):
    """SPEC §3.3 + v0.2 additions.

    We only claim capabilities we actually implement. Advertising a
    flag = ``True`` that isn't wired lies to the TUI; flip to
    ``True`` only once the underlying surface works.
    """

    # v0.1 baseline
    workspaces: bool = False
    sessions: bool = False
    subagents: bool = False
    mcp: bool = False
    lsp: bool = False
    files: bool = False
    diffs: bool = False
    permissions: bool = False
    providers: bool = False
    commands: bool = False
    voice: bool = False
    scheduled_sessions: bool = False
    hooks: bool = False
    session_tasks: bool = False
    metrics: bool = False
    session_branching: bool = False
    session_sharing: bool = False
    session_export: bool = False
    cost_tracking: bool = False
    thinking_blocks: bool = False
    edit_modes: bool = False
    plan_mode: bool = False
    search_messages: bool = False
    agent_write: bool = False
    skills_extraction: bool = False

    # v0.2 additions — SPEC §3.2.1
    agent_routing: bool = False
    memory: bool = False
    structured_errors: bool = False
    integration_health: bool = False
    tool_telemetry: bool = False


class TransportFlags(BaseModel):
    events_sse: bool = True
    events_websocket: bool = False


class AuthInfo(BaseModel):
    schemes: list[str] = Field(default_factory=lambda: ["trust_socket"])
    current: str = "trust_socket"


class Extension(BaseModel):
    id: str
    version: str
    docs: str = ""


class Capabilities(BaseModel):
    """GET /v1/capabilities — SPEC §3.3."""

    contract_version: str = "0.2"
    backend: BackendInfo
    capabilities: CapabilityFlags
    transports: TransportFlags = Field(default_factory=TransportFlags)
    auth: AuthInfo = Field(default_factory=AuthInfo)
    extensions: list[Extension] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §14 — error taxonomy (v0.2)
# ---------------------------------------------------------------------------


class ErrorInfo(BaseModel):
    """Structured error envelope — SPEC §14.

    Carried as the body of HTTP 4xx/5xx responses (wrapped in
    ``{"error": ErrorInfo}``) and on ``Message.error_info`` for
    turn-level errors.
    """

    error: str  # machine-readable taxonomy tag
    message: str  # user-facing description
    details: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False
    retry_after_s: Optional[int] = None


class ErrorEnvelope(BaseModel):
    """SPEC §6.0 wrapper. v0.1 had ``{error: {code, message, details}}``;
    v0.2 carries an ``ErrorInfo`` inside the same wrapper so v0.1
    clients keep deserialising the outer ``error`` object — the
    inner shape just gains fields."""

    error: ErrorInfo


# ---------------------------------------------------------------------------
# §6.19 — memory stats (v0.2)
# ---------------------------------------------------------------------------


class CacheStats(BaseModel):
    """ARC cache counters — SPEC §6.19."""

    hits: int = 0
    misses: int = 0
    hit_rate: float = 0.0  # [0..1]
    capacity: int = 0


class SessionMemoryStats(BaseModel):
    """Per-session context retention — SPEC §6.19. Populated only
    when ?session_id= is supplied."""

    session_id: str
    messages_retained: int = 0
    tokens_retained: int = 0
    tokens_budget: Optional[int] = None  # null = unbounded
    profiles_attached: int = 0


class GlobalMemoryStats(BaseModel):
    """ARC-wide totals — SPEC §6.19."""

    conversations_total: int = 0
    invocations_total: int = 0


class MemoryStats(BaseModel):
    """GET /v1/memory/stats body — SPEC §6.19.

    ``global`` is a Python keyword, so the attribute is spelled
    ``global_``; ``serialization_alias`` maps it to the wire key
    ``global``. FastAPI's response-rendering path uses the alias
    automatically when ``response_model_by_alias=True`` on the
    route (set in app.py).
    """

    cache: CacheStats
    session: Optional[SessionMemoryStats] = None
    global_: GlobalMemoryStats = Field(
        default_factory=GlobalMemoryStats,
        alias="global",
        serialization_alias="global",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# §4 — data model (populated incrementally)
# ---------------------------------------------------------------------------


class Session(BaseModel):
    """GACT v0.2 §4.2. Fields CLIO doesn't populate yet are absent
    on the wire rather than carrying nulls — see SPEC §3.2 on
    clients tolerating missing-optional fields."""

    id: str
    workspace_id: str
    title: str
    status: Literal[
        "idle", "running", "waiting_permission", "error", "cancelled"
    ] = "idle"
    created_at: str
    updated_at: str
    message_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateSessionRequest(BaseModel):
    """POST /v1/sessions body — SPEC §6.2."""

    workspace_id: str = "ws_default"
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListSessionsResponse(BaseModel):
    """GET /v1/sessions body."""

    sessions: list[Session]


# ---------------------------------------------------------------------------
# §4.4 — Message + Part (subset populated by BBB9)
# ---------------------------------------------------------------------------


class Part(BaseModel):
    """SPEC §4.5. The discriminator is ``type``; fields are
    ``omitempty`` JSON-wise so unused ones don't serialise.

    ``id`` is optional on the wire because v0.1 clients may omit it
    when POSTing user parts (they let the server assign an id). The
    server always populates it before emitting on SSE, so readers
    still see a stable id.
    """

    id: str = ""
    type: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    # text / error (v0.1 error part shape)
    text: str = ""

    # routing_decision (v0.2 §4.5)
    selected_agent: str = ""
    rationale: str = ""
    confidence: float = 0.0
    heuristic: bool = False


class Message(BaseModel):
    """SPEC §4.4 + v0.2 error_info. Fields outside BBB9's scope
    (model/tokens/cost/stop_reason/parent etc.) default to zero /
    empty until their endpoints land."""

    id: str
    session_id: str
    role: Literal["user", "assistant", "system", "tool"]
    created_at: str
    updated_at: str
    parts: list[Part] = Field(default_factory=list)
    error_info: Optional[ErrorInfo] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PostMessageRequest(BaseModel):
    """POST /v1/sessions/{sid}/messages body — SPEC §6.3.

    The wire contract uses ``parts[]`` (a list of the same Part
    shape the server emits). We accept ``text`` as a convenience
    alias for the single-text-part form because CLIO's early
    scaffold used that; prefer ``parts`` for new callers.
    """

    parts: list[Part] = Field(default_factory=list)
    text: Optional[str] = None
    model: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def extract_text(self) -> str:
        """Return the user-visible text payload.

        Preference order: the first Part with ``type == "text"``,
        falling back to the legacy ``text`` field when the caller
        used the simpler shape.
        """

        for p in self.parts:
            if p.type == "text" and p.text:
                return p.text
        return self.text or ""


class AgentDef(BaseModel):
    """GACT §6.5 + v0.2 §4.3.1 multi-tier additions.

    Fields CLIO can't populate (system_prompt, default_model,
    parameters, etc.) stay absent from the wire — SPEC §3.2 says
    clients tolerate missing-optional fields.
    """

    id: str
    source: Literal["builtin", "user", "recipe", "skill"] = "builtin"
    title: str
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # v0.2 — multi-tier routing
    tier: int = 0  # 0 = untagged, 1 = orchestrator, 2 = specialist, 3 = nanoagent
    specialization: str = ""
    keywords: list[str] = Field(default_factory=list)


class ListAgentsResponse(BaseModel):
    """GET /v1/agents body."""

    agents: list[AgentDef]


class Tool(BaseModel):
    """SPEC §4.6 (subset). The gateway surfaces a curated set per
    expert; we flatten them into a single catalog for GET
    /v1/catalog/tools."""

    id: str
    source: Literal["builtin", "mcp", "recipe", "extension"] = "builtin"
    server_id: Optional[str] = None
    name: str
    title: str = ""
    description: str = ""
    permission_default: str = "ask"


class ListToolsResponse(BaseModel):
    """GET /v1/catalog/tools body."""

    tools: list[Tool]


class PostMessageResponse(BaseModel):
    """POST /v1/sessions/{sid}/messages response — SPEC §6.3.

    Carries the full assistant turn (one request → one response
    when ``stream`` is false). Streaming lands in BBB10 via SSE on
    /v1/sessions/{sid}/events.
    """

    user_message: Message
    assistant_message: Message


# ---------------------------------------------------------------------------
# §6.16 — /v1/metrics
# ---------------------------------------------------------------------------


class MetricsSessions(BaseModel):
    total: int = 0
    active: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)


class MetricsMessages(BaseModel):
    total: int = 0
    by_role: dict[str, int] = Field(default_factory=dict)


class MetricsTokens(BaseModel):
    input_total: int = 0
    output_total: int = 0
    cache_read_total: int = 0
    cache_write_total: int = 0


class MetricsCost(BaseModel):
    total_usd: float = 0.0
    by_provider: dict[str, float] = Field(default_factory=dict)


class MetricsLatencyStat(BaseModel):
    count: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    max_ms: float = 0.0


class Metrics(BaseModel):
    """GET /v1/metrics body — SPEC §6.16."""

    uptime_s: int
    sessions: MetricsSessions = Field(default_factory=MetricsSessions)
    messages: MetricsMessages = Field(default_factory=MetricsMessages)
    tokens: MetricsTokens = Field(default_factory=MetricsTokens)
    cost: MetricsCost = Field(default_factory=MetricsCost)
    latencies: dict[str, MetricsLatencyStat] = Field(default_factory=dict)
