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
# §4 — data model (populated incrementally)
# ---------------------------------------------------------------------------


# Placeholder for incremental implementation. Workspace / Session /
# Message / Part models land as their endpoints do (CLIO-BBBBBBBBBB7+).
