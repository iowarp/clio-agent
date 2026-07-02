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
    """One subsystem row in ``/v1/health.integrations[]`` (SPEC §3.4)."""

    name: str
    status: str  # "ready" | "degraded" | "unavailable"
    detail: str = ""


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
