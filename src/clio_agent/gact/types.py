"""GACT v0.2 wire types as Pydantic models.

Shapes mirror ``gact-tui/contract/SPEC.md`` (v0.2). This module is
the Python counterpart of the Go types at
``gact-tui/emulator/pkg/gact/``; field names + JSON keys match so
generated client code rounds-trips cleanly.

Only the shapes we actually emit or consume live here — we stub
incrementally as each endpoint lands.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from clio_agent.arc.schema import SegmentKind
from clio_agent.gact.context_preferences_types import (
    ContextPreferences as ContextPreferences,
)
from clio_agent.gact.context_preferences_types import (
    UpdateContextPreferencesRequest as UpdateContextPreferencesRequest,
)
from clio_agent.gact.parts import CapabilityFlags, Part
from clio_agent.gact.workspace_types import (
    CreateWorkspaceRequest as CreateWorkspaceRequest,
)
from clio_agent.gact.workspace_types import (
    ListWorkspacesResponse as ListWorkspacesResponse,
)
from clio_agent.gact.workspace_types import (
    Workspace as Workspace,
)

# ---------------------------------------------------------------------------
# §3 — health + capabilities
# ---------------------------------------------------------------------------


class Integration(BaseModel):
    """One subsystem row in ``/v1/health.integrations[]`` (v0.2 §3.4).

    ``name``/``status``/``detail`` are the v0.2 back-compat triple the TUI's
    ``/doctor`` modal already parses. The richer optional fields (#800) carry the
    full :class:`clio_agent.runtime.status.IntegrationStatus` detail so the single
    doctor engine loses nothing on the wire and the CLI/TUI can render the same
    columns as ``render_doctor_report`` (summary / config source / endpoint / next
    action). They are additive and default to ``None`` so existing readers stay
    valid; ``detail`` mirrors ``summary`` for clients that only read ``detail``.
    """

    name: str
    status: Literal["ready", "degraded", "unavailable"]
    detail: str = ""
    summary: Optional[str] = None
    config_source: Optional[str] = None
    next_action: Optional[str] = None
    endpoint: Optional[str] = None


class HealthResponse(BaseModel):
    """GET /v1/health — SPEC §3.4 with v0.2 integration_health additions."""

    healthy: bool
    uptime_s: int
    overall_status: Optional[Literal["ready", "degraded", "unavailable"]] = None
    integrations: Optional[list[Integration]] = None
    # #772: whether the tool-runtime hooks (permission gate + tool observer) are
    # installed. ``False`` means the install *failed* — tools would run ungated/
    # unobserved, the highest-severity silent fallback, now surfaced so operators
    # can see the degraded gate (the error itself is captured in
    # ``app.state.tool_hooks_install_error`` and logged as
    # ``reason=tool_runtime_hooks_install_failed``). ``None`` means
    # not-yet-determined: deferred agent init hasn't installed the hooks yet
    # (or the agent itself failed to construct — see ``agent_init_error``).
    tool_hooks_installed: Optional[bool] = None


class BackendInfo(BaseModel):
    name: str
    version: str
    vendor: str
    homepage: str = ""


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
    relay: dict[str, Any] = Field(default_factory=dict)


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


class ContextStateResponse(BaseModel):
    """GET /v1/sessions/{sid}/context/state — the ARC live context plane for a scope.

    Two token readings, both model-window-relative (``window_tokens`` from the model's
    resolved context window):

    * ``live_tokens`` / ``pct_used`` — the **segment-store attribution** (sum of
      per-segment ``token_count`` over the live render). This is what the editable
      blocks (``categories`` below, mutated via ``/context/ops``) add up to.
    * ``used_tokens`` / ``used_pct`` — the **model-grounded** reading: the LAST LM call's
      real prompt tokens (provider ``prompt_tokens`` → ``litellm.token_counter`` fallback,
      the same source the in-turn auto-compactor uses). ``null`` between turns / when
      unknown. This is the true "how full is the window" number for the active expert.

    ``autocompact_pct`` is the fraction at which auto-compaction triggers (so a UI can draw
    the line). ``categories`` buckets ``tokens_by_kind`` into Claude-Code-``/context``-style
    groups, plus a ``framing`` entry = ``used_tokens − live_tokens`` (the system-prompt +
    tool-schema overhead the model sees but ARC does not store/edit), when both are known.
    """

    session_id: str
    scope: str
    as_of: Optional[int] = None
    window_tokens: int = 0
    live_tokens: int = 0
    pct_used: Optional[float] = None
    used_tokens: Optional[int] = None
    used_pct: Optional[float] = None
    autocompact_enabled: bool = True
    autocompact_pct: Optional[float] = None
    live_block_count: int = 0
    tokens_by_kind: dict[str, int] = Field(default_factory=dict)
    categories: dict[str, int] = Field(default_factory=dict)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    render_text: str = ""
    render_keys: dict[str, Any] = Field(default_factory=dict)


class ContextOpRequest(BaseModel):
    """POST /v1/sessions/{sid}/context/ops — apply one live-context operation.

    Only the fields relevant to ``op`` are used (append/insert: ``kind`` +
    ``content`` [+ ``position`` for insert]; delete: ``ids``; summarize: ``ids`` +
    ``summary_content``).
    """

    op: Literal["append", "insert", "delete", "summarize"]
    scope: str
    kind: Optional[SegmentKind] = None
    content: Optional[dict[str, Any]] = None
    position: Optional[int] = None
    ids: Optional[list[str]] = None
    summary_content: Optional[dict[str, Any]] = None
    step: int = -1
    token_count: int = 0
    trace_ref: str = ""


class ContextOpResponse(BaseModel):
    """Result of a context op plus a fresh state snapshot so the TUI updates
    without a second GET."""

    session_id: str
    scope: str
    op: str
    applied: bool = True
    result: Optional[dict[str, Any]] = None
    tombstoned_count: Optional[int] = None
    live_block_count: int = 0
    tokens_by_kind: dict[str, int] = Field(default_factory=dict)
    pct_used: Optional[float] = None


class ContextSearchHit(BaseModel):
    """One ranked scope from a context discovery search."""

    scope: str
    score: float


class ContextSearchResponse(BaseModel):
    """GET /v1/sessions/{sid}/context/search — semantic discovery over scopes.

    'which expert/scope knows about X'. ``semantic`` is True for real BM25 (the clio-core
    backend) and False for the naive word-overlap fallback (LocalFS).
    """

    session_id: str
    query: str
    semantic: bool = False
    hits: list[ContextSearchHit] = Field(default_factory=list)


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
    last_interaction_at: str = ""
    message_count: int = 0
    parent_session_id: str = ""
    model: ModelRef = Field(default_factory=ModelRef)
    agent: AgentRef = Field(default_factory=AgentRef)
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    # capabilities.plan_mode + edit_modes. P1.1 #1063 deleted the unenforced ``chat`` mode (== edit).
    mode: Literal["plan", "edit", "architect"] = "edit"
    edit_mode: Literal["diff", "whole", "patch"] = "diff"
    routing_mode: Literal["auto", "chat", "experts", "reasoning_only"] = "auto"
    # iowarp/clio-agent #1034 — approval axis, ORTHOGONAL to ``mode`` (default
    # "ask"). Decides a non-read call at the gate's prompt boundary; the
    # plan/architect lock + reads-never-gated invariant sit ABOVE it (gate docs).
    approval_mode: Literal["ask", "auto-edits", "bypass", "ai-review", "spotter-ai"] = "ask"
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
    mode: Literal["plan", "edit", "architect"] = "edit"  # P1.1 #1063: no ``chat`` (422 rejects it)
    edit_mode: Literal["diff", "whole", "patch"] = "diff"
    routing_mode: Literal["auto", "chat", "experts", "reasoning_only"] = "auto"
    approval_mode: Literal["ask", "auto-edits", "bypass", "ai-review", "spotter-ai"] = "ask"
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateSessionRequest(BaseModel):
    """PATCH /v1/sessions/{sid} body — only the fields the TUI lets
    the user toggle live. Everything is optional; missing fields
    leave the corresponding session attribute alone."""

    title: Optional[str] = None
    model: Optional[ModelRef] = None
    agent: Optional[AgentRef] = None
    mode: Optional[Literal["plan", "edit", "architect"]] = None  # P1.1 #1063: no ``chat``
    edit_mode: Optional[Literal["diff", "whole", "patch"]] = None
    # routing_mode overrides the planner. "auto" runs the normal planner;
    # "chat" forces every turn through chat so users don't need a /chat
    # prefix; "experts" rejects direct chat/none routes; "reasoning_only"
    # asks the planner to prefer tool/expert reasoning over deterministic
    # shortcuts.
    routing_mode: Optional[Literal["auto", "chat", "experts", "reasoning_only"]] = None
    approval_mode: Optional[Literal["ask", "auto-edits", "bypass", "ai-review", "spotter-ai"]] = (
        None  # #1034
    )
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
    # turn_id correlates this message with its user-turn (#711): the agent-lifecycle
    # id that equals the originating user message id. For a user message it equals
    # its own ``id``; for the assistant/system reply it equals the user message id
    # that started the turn. Durable (persisted in the ledger + GET /messages) and
    # the same value carried by ``semantic.event`` payloads, so consumers join the
    # assistant prose stream to the execution trajectory without heuristics. Empty
    # only for messages created outside any active turn.
    turn_id: str = ""
    role: Literal["user", "assistant", "system", "tool"]
    created_at: str
    updated_at: str
    parts: list[Part] = Field(default_factory=list)
    tokens: Tokens = Field(default_factory=Tokens)
    cost_usd: float = 0.0
    stop_reason: str = ""
    error_info: Optional[ErrorInfo] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        """Project this message to its on-the-wire dict with slimmed parts.

        The message envelope (tokens/cost/stop_reason/metadata) keeps its
        ``exclude_none`` shape so existing readers see the same top-level keys;
        only the nested ``parts`` are projected through :meth:`Part.to_wire` so
        they drop their unused per-type fields.
        """

        wire = self.model_dump(exclude_none=True)
        wire["parts"] = [part.to_wire() for part in self.parts]
        return wire


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

        Preference order: text parts joined in order, falling back to the
        legacy ``text`` field when the caller used the simpler shape.
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
    # Per-expert provider identity (#818). All data, never inline secrets: an
    # empty value means "inherit the default profile" (today's behaviour).
    api_base: str = ""  # explicit endpoint override for this expert's provider
    credential_ref: str = ""  # KEY into a credential source (e.g. "openai:acctB"), never a secret
    transport: str = ""  # transport hint: Codex/Claude Code both use their SDK
    parameters: dict[str, Any] = Field(default_factory=dict)
    module: dict[str, Any] = Field(default_factory=dict)
    signature: dict[str, Any] = Field(default_factory=dict)
    structured_outputs: dict[str, Any] = Field(default_factory=dict)
    fanout: dict[str, Any] = Field(default_factory=dict)
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
# /v1/providers/lm — TUI-side LM config
# ---------------------------------------------------------------------------


class LMProviderInfo(BaseModel):
    """GET /v1/providers/lm body: current LM config state + the preset list the
    TUI's picker shows. ``api_key`` is never echoed back.
    ``thinking_level`` (off|low|medium|high, null=unset) is the provider-generic
    reasoning control; ``thinking_budget`` is the explicit token override; and
    ``thinking_effective`` is the resolved per-provider effect (#895), so the
    knob is never invisible on the wire — including a typed ``unsupported`` note.
    """

    configured: bool
    provider: str = ""
    api_base: str = ""
    model: str = ""
    # Mirrors LMProviderConfig's deterministic default (see config.py):
    # the agentic LM path is structured/tool-calling, so greedy decoding
    # is the sane default. Overridable from the TUI.
    temperature: float = 0.0
    max_tokens: int = 32000
    context_length: int = 0
    # Handshake-discovered, queryable. ``chosen_context`` is the active context
    # limit clio operates against (the "context budget" other subsystems query);
    # ``context_window`` is the model's hard ceiling; the capability flags reflect
    # what the provider reported (reasoning model / native tool-calling).
    chosen_context: Optional[int] = None
    context_window: Optional[int] = None
    is_reasoning: bool = False
    native_tool_calling: bool = False
    thinking_level: Optional[str] = None
    thinking_effective: str = ""
    thinking_budget: int = 0
    transport: Optional[Literal["sdk"]] = None
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
    (temperature=0.0 — deterministic, structured/tool-calling agentic
    output; max_tokens=32000).
    """

    provider: str
    api_base: str
    model: str
    api_key: str = "x"
    temperature: float = 0.0
    max_tokens: int = 0
    # Sampling surface forwarded to dspy.LM/LiteLLM (None = omit -> the model's own
    # default). Greedy decoding (temperature 0) makes Qwen-family reasoning models
    # degenerate into endless repetition; Qwen recommends temp 0.6 / top_p 0.95 /
    # top_k 20 for thinking mode. top_p/presence_penalty are OpenAI-standard;
    # top_k/min_p are forwarded via extra_body (llama.cpp / LM Studio / vLLM).
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    # Context window requested/expected for the model load. This is
    # not forwarded to DSPy/LiteLLM as a completion parameter; local
    # runtimes such as LM Studio must load the model with this context
    # separately.
    context_length: int = 0
    # Max concurrent predictions the local backend may run at once
    # (LM Studio's load-time ``parallel`` field, the UI's "Max Concurrent
    # Predictions"). The agent issues parallel sub-calls; a single-GPU box
    # OOMs/stalls when the backend serves them concurrently, so this caps
    # backend concurrency at load time. 0 = clio's default (1 for LM Studio,
    # so concurrent pipeline calls queue instead of thrashing the GPU).
    parallel: int = 0
    # Per-turn no-progress watchdog (seconds): bounds the gap between observable
    # progress events within one turn, NOT total duration. Exposed here so a
    # client (e.g. the test harness) can drive it on the SAME channel it
    # configures the LM, instead of it being a disconnected server-launch env.
    # 0 = unset → fall back to conf `limits.turn_timeout_s` /
    # CLIO_GACT_TURN_TIMEOUT_S / 900s default. Slow reasoning models over a long
    # multi-stage pipeline need ~1800.
    turn_timeout_s: float = 0.0
    # A string lets the provider validator return a typed 400 for deleted transports.
    transport: Optional[str] = None
    # Reasoning knobs, mapped per-provider in providers.thinking (#895).
    # thinking_level (off|low|medium|high, null=unset → shipped per-model default)
    # is the provider-generic control (budget_tokens for anthropic/claude_code,
    # reasoning_effort for openai/codex); an invalid string is a structured 422 and
    # a provider with no mapping surfaces a typed ``unsupported`` in the GET.
    # thinking_budget is the explicit token override (0 = defer to the level).
    thinking_level: Optional[Literal["off", "low", "medium", "high"]] = None
    thinking_budget: int = 0
