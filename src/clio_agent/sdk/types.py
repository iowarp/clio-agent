"""Typed wire shapes for the GACT v1 API, as a *client* sees them.

These models are re-declared here (rather than imported from
``clio_agent.gact.types``) on purpose: importing the server's types
module drags in the whole gact server package (FastAPI app, ARC
schema, …), and the SDK must stay a pure client. Field names and JSON
keys mirror the reconciled contract (``gact-tui/contract/SPEC.md``)
and ``clio_agent/gact/types.py`` exactly; only the client-relevant
subset is declared.

Forward compatibility (SPEC §2): every model tolerates and preserves
unknown fields (``extra="allow"``), so a newer backend never breaks
deserialization.

Example:
    >>> sess = Session.model_validate({"id": "sess_1", "workspace_id": "ws_default",
    ...                                "title": "t", "created_at": "…", "updated_at": "…"})
    >>> sess.status
    'idle'
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _WireModel(BaseModel):
    """Base for all SDK wire shapes: unknown fields are preserved."""

    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------------- #
# §14 — error envelope
# --------------------------------------------------------------------------- #


class ErrorInfo(_WireModel):
    """SPEC §14.1 typed error envelope.

    ``error`` is the machine-readable taxonomy tag (open set); v0.1
    backends called it ``code`` — the SDK's error mapper tolerates both.
    """

    error: str = ""
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False
    retry_after_s: int | None = None


# --------------------------------------------------------------------------- #
# §3 — health + capabilities
# --------------------------------------------------------------------------- #


class Integration(_WireModel):
    """One subsystem row in ``/v1/health.integrations[]`` (SPEC §3.4).

    ``name``/``status``/``detail`` are the back-compat triple every
    reader already parses. The richer optional fields (#800) carry the
    full doctor detail (``clio_agent.gact.types.Integration`` widened it
    at the wire) so ``client.health().integrations`` renders the same
    columns as the server's ``render_doctor_report`` — summary, config
    source, endpoint, next action. All additive and default ``None`` so
    older backends (which omit them) still deserialize.
    """

    name: str
    status: str  # "ready" | "degraded" | "unavailable"
    detail: str = ""
    summary: str | None = None
    config_source: str | None = None
    next_action: str | None = None
    endpoint: str | None = None


class Health(_WireModel):
    """GET /v1/health — SPEC §3.4.

    Note the carve-out: an unavailable backend answers 503 with this
    body (not an error envelope); the SDK returns it typed either way.
    """

    healthy: bool
    uptime_s: int = 0
    overall_status: str | None = None
    integrations: list[Integration] = Field(default_factory=list)


class BackendInfo(_WireModel):
    name: str = ""
    version: str = ""
    vendor: str = ""
    homepage: str = ""


class TransportFlags(_WireModel):
    events_sse: bool = False
    events_websocket: bool = False


class AuthInfo(_WireModel):
    schemes: list[str] = Field(default_factory=list)
    current: str = ""


class Capabilities(_WireModel):
    """GET /v1/capabilities — SPEC §3.3.

    ``capabilities`` is kept as an open mapping (flags are an open,
    vendor-extensible set); probe it through :meth:`supports` which
    encodes the capability-truth rule: a flag absent from the map is
    unsupported, exactly like a flag advertised ``False``.

    Example:
        >>> caps = client.capabilities()
        >>> if caps.supports("session_branching"):
        ...     client.sessions.fork(sid)
    """

    contract_version: str = ""
    backend: BackendInfo = Field(default_factory=BackendInfo)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    transports: TransportFlags = Field(default_factory=TransportFlags)
    auth: AuthInfo = Field(default_factory=AuthInfo)
    extensions: list[dict[str, Any]] = Field(default_factory=list)

    def supports(self, flag: str) -> bool:
        """True only when ``flag`` is advertised truthy (SPEC §3.3).

        Vendor ``x_clio_*`` flags may carry non-boolean values (e.g.
        ``"best_effort"``); any truthy value counts as supported.
        """

        return bool(self.capabilities.get(flag, False))

    def flag(self, name: str, default: Any = None) -> Any:
        """Raw capability value (for richer-than-boolean vendor flags)."""

        return self.capabilities.get(name, default)


# --------------------------------------------------------------------------- #
# §4.1 — workspaces
# --------------------------------------------------------------------------- #


class Workspace(_WireModel):
    """SPEC §4.1 — a project root grouping sessions."""

    id: str
    name: str = ""
    root_path: str = ""
    storage_root: str = ""
    created_at: str = ""
    updated_at: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# §4.2 — sessions
# --------------------------------------------------------------------------- #


class ModelRef(_WireModel):
    provider_id: str = ""
    model_id: str = ""
    variant: str = ""


class AgentRef(_WireModel):
    id: str = "main"
    mode: str = ""


class Session(_WireModel):
    """SPEC §4.2 (implemented shape: flattened token rollups,
    zero-value defaults, three mode fields)."""

    id: str
    workspace_id: str = ""
    parent_session_id: str = ""
    title: str = ""
    status: str = "idle"
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    model: ModelRef = Field(default_factory=ModelRef)
    agent: AgentRef = Field(default_factory=AgentRef)
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    mode: str = "chat"
    edit_mode: str = "diff"
    routing_mode: str = "auto"
    archived: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# §4.4 / §4.5 — messages + parts
# --------------------------------------------------------------------------- #


class Part(_WireModel):
    """SPEC §4.5 — one flat struct, discriminated by ``type``.

    The backend serializes parts "omitempty" style: only fields the
    part actually set are on the wire, plus the always-kept identity
    triple (``id``/``type``/``agent_id``). Declared here are the
    fields common across the core part types; anything else (e.g.
    ``unified_diff`` on a ``file_diff``) is preserved via
    ``extra="allow"`` and reachable as an attribute.
    """

    id: str = ""
    type: str
    agent_id: str = ""
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    # tool_call / tool_result
    call_id: str = ""
    tool_name: str = ""
    thought: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    content: list[Part] = Field(default_factory=list)
    is_error: bool = False
    cached: bool = False
    duration_ms: float = 0.0

    # routing_decision
    selected_agent: str = ""
    rationale: str = ""
    execution_path: str = ""


class Tokens(_WireModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


class Message(_WireModel):
    """SPEC §4.4 + v0.2 ``error_info``."""

    id: str
    session_id: str = ""
    turn_id: str = ""
    role: str
    created_at: str = ""
    updated_at: str = ""
    parts: list[Part] = Field(default_factory=list)
    tokens: Tokens = Field(default_factory=Tokens)
    cost_usd: float = 0.0
    stop_reason: str = ""
    error_info: ErrorInfo | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def text(self) -> str:
        """All ``text`` part content joined in wire order."""

        return "\n".join(p.text for p in self.parts if p.type == "text" and p.text)


class PostMessageAck(_WireModel):
    """POST /v1/sessions/{sid}/messages ack (SPEC §6.3): the turn
    itself streams asynchronously over SSE."""

    message_id: str
    accepted_at: str = ""


# --------------------------------------------------------------------------- #
# §4.7 / §6.11 — permissions
# --------------------------------------------------------------------------- #


class PermissionToolCall(_WireModel):
    tool_name: str = ""
    input: dict[str, Any] = Field(default_factory=dict)


class PermissionRequest(_WireModel):
    """SPEC §4.7 — the thin implemented row. Resolution fields
    (``action``/``resolved_at``/``reason``/``policy``) appear only
    once the row leaves ``pending``."""

    id: str
    session_id: str = ""
    tool_call: PermissionToolCall = Field(default_factory=PermissionToolCall)
    summary: str = ""
    created_at: str = ""
    status: str = "pending"
    action: str = ""
    resolved_at: str = ""
    reason: str = ""


class PermissionList(_WireModel):
    """GET /v1/permissions — rows (desc by created_at) + list metadata."""

    permissions: list[PermissionRequest] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# §6.5 — agent catalog
# --------------------------------------------------------------------------- #


class Agent(_WireModel):
    """One row in ``GET /v1/agents`` (SPEC §6.5 + v0.2 §4.3.1).

    Mirrors the client-relevant subset of the server's ``AgentDef``:
    the display triple (``id``/``title``/``description``), the routing
    metadata the CLI's ``/experts`` and ``/registry`` render (``tier``/
    ``specialization``/``keywords``), and the curated surface lists.
    Anything richer on the wire (e.g. ``capability_refs``, ``module``,
    ``signature``) is preserved via ``extra="allow"``.

    Note the wire key is ``title``, not ``name`` — the CLI shows
    :attr:`title` (falling back to :attr:`id`) as the agent's name.
    """

    id: str
    source: str = "builtin"
    title: str = ""
    description: str = ""
    parent_id: str = ""
    tier: int = 0
    specialization: str = ""
    keywords: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# §6.5 / §6.6 — unified tool catalog
# --------------------------------------------------------------------------- #


class Tool(_WireModel):
    """One row in the unified ``GET /v1/tools`` catalog (SPEC §6.5).

    Flattens every tool the bundled in-process gateway and any installed
    third-party MCP servers expose. ``server_id``/``source`` say where a
    tool comes from; error rows (a server that failed introspection) set
    ``source="error"`` and carry the reason in ``description`` — surfaced,
    never dropped (no silent fallback).
    """

    id: str = ""
    name: str = ""
    description: str = ""
    server_id: str = ""
    source: str = ""
    permission_default: str = "ask"
    owner: str = ""
    tags: list[str] = Field(default_factory=list)
    visible_to: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# §6.16 — /v1/metrics
# --------------------------------------------------------------------------- #


class MetricsSessions(_WireModel):
    total: int = 0
    active: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)


class MetricsMessages(_WireModel):
    total: int = 0
    by_role: dict[str, int] = Field(default_factory=dict)


class MetricsTokens(_WireModel):
    input_total: int = 0
    output_total: int = 0
    cache_read_total: int = 0
    cache_write_total: int = 0


class MetricsCost(_WireModel):
    total_usd: float = 0.0
    by_provider: dict[str, float] = Field(default_factory=dict)


class MetricsLatencyStat(_WireModel):
    count: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    max_ms: float = 0.0


class Metrics(_WireModel):
    """GET /v1/metrics body — SPEC §6.16. Aggregate runtime counters."""

    uptime_s: int = 0
    sessions: MetricsSessions = Field(default_factory=MetricsSessions)
    messages: MetricsMessages = Field(default_factory=MetricsMessages)
    tokens: MetricsTokens = Field(default_factory=MetricsTokens)
    cost: MetricsCost = Field(default_factory=MetricsCost)
    latencies: dict[str, MetricsLatencyStat] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# §6 — /v1/providers/lm (LM config)
# --------------------------------------------------------------------------- #


class LMProviderPreset(_WireModel):
    """One row in the provider picker (subset of the server's shape)."""

    id: str = ""
    label: str = ""
    provider: str = ""
    api_base: str = ""
    suggested_model: str = ""
    requires_api_key: bool = True
    auth_method: str = "api_key"
    is_authenticated: bool = False
    description: str = ""
    status: str = "unknown"
    status_message: str = ""


class LMProvider(_WireModel):
    """GET /v1/providers/lm body (server type ``LMProviderInfo``).

    Reports the live LM config the CLI's ``/models`` renders: whether an
    agent is wired (``configured``), the bound provider/model/endpoint,
    the sampling + context budget, and the enumerated ``presets`` the
    picker offers. ``api_key`` is never on the wire.
    """

    configured: bool = False
    provider: str = ""
    api_base: str = ""
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 0
    context_length: int = 0
    chosen_context: int | None = None
    context_window: int | None = None
    is_reasoning: bool = False
    native_tool_calling: bool = False
    thinking_budget: int = 0
    transport: str | None = None
    state: str = "idle"
    status_message: str = ""
    error: str = ""
    operation_id: str = ""
    presets: list[LMProviderPreset] = Field(default_factory=list)
