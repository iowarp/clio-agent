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
    session_summary: bool = (
        False  # POST /sessions/{id}/summarize — user-facing TLDR (distinct from compact)
    )
    attachments_upload: bool = False  # POST /sessions/{id}/attachments — base64 byte upload
    multimodal_image_parts: bool = False  # POST /messages accepts/preserves image parts
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

    # CLIO vendor truth flags. GACT conformance treats x_* keys as
    # vendor metadata, so these can carry richer values than booleans.
    x_clio_cancellation: Literal["none", "best_effort", "hard"] = "none"
    x_clio_executor_cancellation: bool = False
    x_clio_text_streaming: Literal["none", "batch", "best_effort_live"] = "none"
    x_clio_synthetic_posthoc_streaming: bool = False
    x_clio_stream_fallback_reasons: dict[str, dict[str, Any]] = Field(default_factory=dict)
    x_clio_direct_delete_permissions: bool = False
    x_clio_prompt_registry: bool = False
    x_clio_expert_packs: bool = False
    x_clio_agent_blueprints: bool = False
    x_clio_user_questions: bool = False
    x_clio_retry_attempts: bool = False
    x_clio_context_frames: bool = False
    x_clio_semantic_events: bool = False
    x_clio_semantic_trace_backend: str = ""
    x_clio_semantic_trace_detail: str = ""
    x_clio_hook_backend: str = ""
    x_clio_hook_events: dict[str, Any] = Field(default_factory=dict)
    x_clio_files_content: bool = (
        False  # GET /sessions/{id}/context/files/content — base64-JSON byte previews
    )
    x_clio_capability_gaps: dict[str, dict[str, Any]] = Field(default_factory=dict)


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
    context_files_attached: int = 0
    context_files_by_mode: dict[str, int] = Field(default_factory=dict)
    compact_summaries: int = 0
    token_pressure: float = 0.0
    threshold_state: Literal["empty", "normal", "warning", "critical"] = "empty"
    compaction_recommended: bool = False


class SessionContextPolicy(BaseModel):
    """Effective context and memory policy for one session.

    This is intentionally separate from ``Session.metadata`` so clients can
    discover CLIO's current memory compartment semantics without reverse
    engineering ad-hoc metadata keys.
    """

    session_id: str
    memory_scope: Literal["session"] = "session"
    writable_scope: Literal["session"] = "session"
    cross_session_read_available: bool = False
    cross_session_read_endpoint: Optional[str] = None
    requires_user_consent: bool = True
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class MemorySearchHit(BaseModel):
    """One transcript-memory match.

    This is intentionally provenance-heavy so a future orchestrator or TUI can
    show where cross-session memory came from before using it as context.
    """

    session_id: str
    session_title: str = ""
    workspace_id: str = ""
    message_id: str
    part_id: str = ""
    role: Literal["user", "assistant", "system", "tool"]
    created_at: str
    updated_at: str = ""
    text: str
    score: float = 0.0
    match_terms: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySearchResponse(BaseModel):
    """GET /v1/memory/search response."""

    query: str
    include_cross_session: bool = False
    searched_sessions: list[str] = Field(default_factory=list)
    hits: list[MemorySearchHit] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextFrameItem(BaseModel):
    kind: str
    source_id: str = ""
    role: str = ""
    path: str = ""
    display_path: str = ""
    included: bool = True
    reason: str = ""
    tokens_estimated: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextFrame(BaseModel):
    id: str
    session_id: str
    turn_id: str = ""
    user_message_id: str = ""
    assistant_message_id: str = ""
    created_at: str
    updated_at: str
    status: Literal["assembled", "context_error", "completed", "error", "cancelled"] = "assembled"
    model: dict[str, str] = Field(default_factory=dict)
    agent: dict[str, Any] = Field(default_factory=dict)
    prompt: dict[str, Any] = Field(default_factory=dict)
    items: list[ContextFrameItem] = Field(default_factory=list)
    tokens_estimated: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# §4 — data model (populated incrementally)
# ---------------------------------------------------------------------------


class Workspace(BaseModel):
    """SPEC §4.1 — a workspace groups related sessions and pins
    a filesystem root the agent's tools are allowed to touch.

    For CLIO each workspace maps to a directory the user has
    explicitly added (think "git project root"). The agent's
    file-policy receives ``root_path`` as part of CLIO_ALLOWED_ROOTS;
    the ARC namespace + session bucket are scoped to ``id``.
    """

    id: str
    name: str
    root_path: str = ""
    storage_root: str = ""
    created_at: str
    updated_at: str
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateWorkspaceRequest(BaseModel):
    """POST /v1/workspaces body."""

    name: str
    root_path: str = ""
    storage_root: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListWorkspacesResponse(BaseModel):
    """GET /v1/workspaces body."""

    workspaces: list[Workspace]


class ModelRef(BaseModel):
    """Per-session or per-message model selection reference."""

    provider_id: str = ""
    model_id: str = ""
    variant: str = ""


class AgentRef(BaseModel):
    """Per-session agent/persona selection reference."""

    id: str = "main"
    mode: str = ""


class Session(BaseModel):
    """GACT v0.2 §4.2. Fields CLIO doesn't populate yet are absent
    on the wire rather than carrying nulls — see SPEC §3.2 on
    clients tolerating missing-optional fields."""

    id: str
    workspace_id: str
    title: str
    status: Literal[
        "idle",
        "running",
        "waiting_permission",
        "waiting_user",
        "error",
        "cancelled",
    ] = "idle"
    created_at: str
    updated_at: str
    message_count: int = 0
    parent_session_id: str = ""
    model: ModelRef = Field(default_factory=ModelRef)
    agent: AgentRef = Field(default_factory=AgentRef)
    # CLIO-BBBBBBBBBB24: cumulative rollups.
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    # iowarp/clio-agent — capabilities.plan_mode + edit_modes:
    mode: Literal["chat", "plan", "edit", "architect"] = "chat"
    edit_mode: Literal["diff", "whole", "patch"] = "diff"
    routing_mode: Literal["auto", "chat", "experts", "reasoning_only"] = "auto"
    metadata: dict[str, Any] = Field(default_factory=dict)
    # iowarp/gact-tui §audit/E-14: lets the desktop and TUI archive a
    # session for "hide from the active list, keep around for browse".
    # Default false so existing sessions stay visible.
    archived: bool = False


class CreateSessionRequest(BaseModel):
    """POST /v1/sessions body — SPEC §6.2."""

    workspace_id: str = "ws_default"
    title: str = ""
    model: Optional[ModelRef] = None
    agent: Optional[AgentRef] = None
    mode: Literal["chat", "plan", "edit", "architect"] = "chat"
    edit_mode: Literal["diff", "whole", "patch"] = "diff"
    routing_mode: Literal["auto", "chat", "experts", "reasoning_only"] = "auto"
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateSessionRequest(BaseModel):
    """PATCH /v1/sessions/{sid} body — only the fields the TUI lets
    the user toggle live. Everything is optional; missing fields
    leave the corresponding session attribute alone."""

    title: Optional[str] = None
    model: Optional[ModelRef] = None
    agent: Optional[AgentRef] = None
    mode: Optional[Literal["chat", "plan", "edit", "architect"]] = None
    edit_mode: Optional[Literal["diff", "whole", "patch"]] = None
    # routing_mode overrides the planner. "auto" runs the normal planner;
    # "chat" forces every turn through chat so users don't need a /chat
    # prefix; "experts" rejects direct chat/none routes; "reasoning_only"
    # asks the planner to prefer tool/expert reasoning over deterministic
    # shortcuts.
    routing_mode: Optional[Literal["auto", "chat", "experts", "reasoning_only"]] = None
    # iowarp/gact-tui §audit/E-14: the desktop needs to push pin state
    # (`metadata.pinned: bool`) and archive state. Without these the
    # desktop's controls flip the UI optimistically but the changes are
    # lost on the next refresh.
    metadata: Optional[dict[str, Any]] = None
    archived: Optional[bool] = None


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

    # image (CLIO extension for multimodal user content). A client may send
    # data or url; the server preserves the fields on the
    # transcript and validates provider support before scheduling the turn.
    data: Optional[str] = None
    url: Optional[str] = None
    media_type: Optional[str] = None

    # routing_decision (v0.2 §4.5)
    selected_agent: str = ""
    rationale: str = ""
    confidence: float = 0.0
    heuristic: bool = False
    # iowarp/clio-agent#25: which path the selected expert actually
    # ran on. "fast" = deterministic tool template (no LM); "expert_
    # loop" = full DSPy ReAct iteration with the expert's tool set.
    # Empty when not applicable (e.g. chat path, or branches that
    # haven't been migrated to the classifier yet — analysis /
    # visualization will follow in their own issues). Field values
    # are user-neutral per CLAUDE.md Rule 3 (no DSPy terms in user-
    # facing payload).
    execution_path: str = ""

    # tool_call / tool_result. CLIO emits these as live SSE parts when
    # MCP tools start/finish so clients can show progress before the
    # final assistant message metadata is attached.
    call_id: str = ""
    tool_name: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    content: list["Part"] = Field(default_factory=list)
    is_error: bool = False
    cached: bool = False
    duration_ms: float = 0.0

    # file_diff (BBB21 + #4): a proposed edit awaiting apply/reject.
    # ``new_content`` (when present) is what the apply path writes
    # to disk — re-applying a unified diff is fragile so we ship
    # the whole-file replacement alongside the diff.
    path: str = ""
    unified_diff: str = ""
    new_content: str = ""
    status: str = ""  # "pending" | "applied" | "rejected" | "apply_failed"
    # iowarp/clio-agent — capabilities.edit_modes: which mode the
    # session was in when this diff was produced. "diff" = unified_diff
    # is the canonical view; "whole" = render new_content full;
    # "patch" = both fields meaningful. Read by the TUI for rendering.
    edit_mode: str = ""
    lines_added: int = 0
    lines_removed: int = 0


class ContextFile(BaseModel):
    """SPEC §6.9 — a file pinned into a session's context."""

    path: str
    mode: Literal["edit", "read", "pin"] = "read"
    added_at: str
    last_modified: str = ""
    size: int = 0
    language: str = ""


class Tokens(BaseModel):
    """Per-message token counts — SPEC §4.4."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


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
    tokens: Tokens = Field(default_factory=Tokens)
    cost_usd: float = 0.0
    stop_reason: str = ""
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
    model: Optional[ModelRef] = None
    agent: Optional[AgentRef] = None
    agent_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def extract_text(self) -> str:
        """Return the user-visible text payload.

        Preference order: text parts joined in order, falling back to the legacy
        ``text`` field when the caller used the simpler shape.
        """

        text_parts: list[str] = []
        for p in self.parts:
            if p.type == "text" and p.text:
                text_parts.append(p.text)
        if text_parts:
            return "\n".join(text_parts).strip()
        return self.text or ""

    def image_parts(self) -> list[Part]:
        """Return image parts supplied in this message."""

        return [p for p in self.parts if p.type == "image"]

    def extract_agent_id(self) -> str:
        """Return a per-turn agent override, if the caller supplied one."""

        if self.agent is not None and self.agent.id:
            return self.agent.id
        return self.agent_id or ""


class AgentCapabilityRef(BaseModel):
    """Normalized agent-visible capability.

    ``tools`` remains as the compact legacy list for existing clients. This
    richer shape lets a TUI distinguish tools, skills, and slash commands
    without scraping metadata or guessing from names.
    """

    kind: Literal["tool", "skill", "command"]
    id: str
    title: str = ""
    description: str = ""
    source: str = "builtin"
    status: Literal["available", "unavailable", "unknown"] = "available"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentDef(BaseModel):
    """GACT §6.5 + v0.2 §4.3.1 multi-tier additions.

    Prompt/provider/model fields are explicit so clients can inspect
    and persist user or skill agent definitions without hiding their
    execution semantics in metadata.
    """

    id: str
    source: Literal["builtin", "user", "recipe", "skill", "expert_pack"] = "builtin"
    title: str
    description: str = ""
    parent_id: str = ""
    system_prompt: str = ""
    prompt_id: str = ""
    prompt_profile: str = ""
    default_provider: str = ""
    default_model: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    capability_refs: list[AgentCapabilityRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    validation_errors: list[str] = Field(default_factory=list)

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
    owner: str = ""
    tags: list[str] = Field(default_factory=list)
    visible_to: list[str] = Field(default_factory=list)


class ListToolsResponse(BaseModel):
    """GET /v1/catalog/tools body."""

    tools: list[Tool]


class PostMessageResponse(BaseModel):
    """POST /v1/sessions/{sid}/messages response — SPEC §6.3.

    Returns immediately with an ack: just the user message id and
    accepted_at timestamp. The assistant turn arrives asynchronously
    via the SSE channel (message.created, message.part.added,
    message.part.delta, message.completed). This shape matches the
    TUI's ``PostMessageResponse`` Go struct + the emulator's
    behaviour so the wire is interoperable.

    The full Message objects are still discoverable via
    GET /v1/sessions/{sid}/messages once the turn settles.
    """

    message_id: str
    accepted_at: str


# ---------------------------------------------------------------------------
# CLIO ask-user and retry protocol (#333)
# ---------------------------------------------------------------------------


class UserQuestionOption(BaseModel):
    label: str
    value: str = ""
    description: str = ""


class UserQuestion(BaseModel):
    id: str
    session_id: str
    prompt: str
    status: Literal["pending", "answered", "cancelled", "expired"] = "pending"
    kind: Literal["freeform", "choice", "confirmation"] = "freeform"
    options: list[UserQuestionOption] = Field(default_factory=list)
    created_at: str
    updated_at: str
    expires_at: str = ""
    source: str = "orchestrator"
    turn_id: str = ""
    attempt_id: str = ""
    answer: str = ""
    selected_options: list[str] = Field(default_factory=list)
    answer_metadata: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateUserQuestionRequest(BaseModel):
    prompt: str
    kind: Literal["freeform", "choice", "confirmation"] = "freeform"
    options: list[UserQuestionOption] = Field(default_factory=list)
    source: str = "orchestrator"
    turn_id: str = ""
    attempt_id: str = ""
    expires_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnswerUserQuestionRequest(BaseModel):
    answer: str = ""
    selected_options: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetryTurnRequest(BaseModel):
    notes: str = ""
    execute: bool = False
    model: Optional[ModelRef] = None
    provider_id: str = ""
    model_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnAttempt(BaseModel):
    id: str
    session_id: str
    source_message_id: str
    status: Literal["recorded", "queued", "running", "completed", "failed", "cancelled"] = (
        "recorded"
    )
    created_at: str
    updated_at: str
    notes: str = ""
    model: ModelRef = Field(default_factory=ModelRef)
    warning: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


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


# ---------------------------------------------------------------------------
# /v1/providers/lm — TUI-side LM config (CLIO-BBBBBBBBBB-D)
# ---------------------------------------------------------------------------


class LMProviderInfo(BaseModel):
    """GET /v1/providers/lm body. Reports current LM config state
    + an enumerated list of presets the TUI can show in its
    provider picker. ``api_key`` is never echoed back; the TUI
    asks the user fresh on every change.

    ``thinking_budget`` controls reasoning effort/budget on providers
    that support it (Anthropic Sonnet 4.6+ extended thinking,
    OpenAI/Codex `model_reasoning_effort`). 0 means default / disabled.
    """

    configured: bool
    provider: str = ""
    api_base: str = ""
    model: str = ""
    temperature: float = 1.0
    max_tokens: int = 32000
    context_length: int = 0
    thinking_budget: int = 0
    transport: Optional[Literal["exec", "sdk"]] = None
    state: Literal["idle", "configuring", "ready", "error"] = "idle"
    status_message: str = ""
    error: str = ""
    operation_id: str = ""
    presets: list["LMProviderPreset"] = Field(default_factory=list)


class LMProviderPreset(BaseModel):
    """One row in the TUI's provider picker. ``requires_api_key``
    tells the modal whether to render the api_key field; some
    presets (LM Studio, Ollama, local vLLM) don't need one."""

    id: str
    label: str
    provider: str
    api_base: str
    suggested_model: str
    requires_api_key: bool = True
    api_key_env: str = ""
    auth_method: Literal["none", "api_key", "oauth"] = "api_key"
    is_authenticated: bool = False
    description: str = ""
    status: Literal[
        "ready",
        "missing_key",
        "auth_required",
        "auth_check_required",
        "unavailable",
        "unknown",
    ] = "unknown"
    status_message: str = ""
    supports_live_catalog: bool = True
    supports_vision: bool = False


class LMProviderRequest(BaseModel):
    """PUT /v1/providers/lm body. Provider is one of
    `openai|anthropic|openrouter|lm_studio|ollama|...` — anything
    LiteLLM understands. ``api_key`` is required for cloud
    providers; locally-OpenAI-compatible backends (LM Studio,
    Ollama, local vLLM) tolerate any non-empty string.

    ``temperature`` + ``max_tokens`` are forwarded to dspy.LM so
    the user can tune behaviour from the TUI without touching env
    vars. Defaults match LMProviderConfig's defaults
    (temperature=1.0, max_tokens=32000).
    """

    provider: str
    api_base: str
    model: str
    api_key: str = "x"
    temperature: float = 1.0
    max_tokens: int = 0
    # Context window requested/expected for the model load. This is
    # not forwarded to DSPy/LiteLLM as a completion parameter; local
    # runtimes such as LM Studio must load the model with this context
    # separately.
    context_length: int = 0
    # Codex-only transport selector. Other providers ignore it.
    transport: Optional[Literal["exec", "sdk"]] = None
    # Reasoning/thinking budget. Mapped per-provider:
    # - Anthropic (haiku/sonnet/opus 4.6+): used as
    #   thinking.budget_tokens in the API call. 0 disables extended
    #   thinking. Range: ~1024..32000 typically.
    # - OpenAI/Codex: mapped to model_reasoning_effort by bucketing
    #   (0 = none, <2000 = low, <8000 = medium, >=8000 = high).
    # - Other providers: ignored.
    thinking_budget: int = 0
