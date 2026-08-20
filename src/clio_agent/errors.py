"""
ClioAgent Structured Error Handling

Structured error types with JSON-serializable output.
Raw tracebacks never reach the user -- all failures are represented as
structured errors instead of fallback assistant responses.

Error hierarchy:
    ClioError (base)
    ├── ProviderError  -- LM provider unavailable/timeout
    ├── RoutingError   -- Router failed to classify
    ├── ExpertError    -- Expert execution failed
    ├── ToolError      -- MCP tool call failed
    ├── ConfigError    -- Configuration invalid
    └── CancellationError -- User-requested cancellation
"""

from __future__ import annotations

from typing import Any

MCP_CAPABILITY_REFUSED = "mcp_capability_refused"
MCP_PROTOCOL_REFUSED = "mcp_protocol_refused"
MCP_RESULT_DOWNGRADED_TO_COMPLETE = "mcp_result_downgraded_to_complete"
MCP_WIRE_CANCELLATION_UNAVAILABLE = "mcp_wire_cancellation_unavailable"
#: #1114: the modern-era MRTR loop (InputRequiredResult -> retry with inputResponses)
#: exceeded its config-resolved round bound without reaching a terminal result.
MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED = "mcp_input_required_rounds_exceeded"
#: #1115: no durable task-record home is published, so SEP-2663 task ids survive
#: losing the client but not the process (reconnect-after-crash is degraded).
MCP_TASK_RECORD_STORE_ABSENT = "mcp_task_record_store_absent"
#: #1115: a task kept reporting ``input_required`` while every surfaced input key
#: was already answered — the drive stops rather than polling forever.
MCP_TASK_INPUT_NO_PROGRESS = "mcp_task_input_no_progress"
#: #1115: the tasks extension was NOT declared on this client because its class
#: forbids internal extensions (a proxy backend must not advertise task support).
MCP_TASKS_DECLARATION_SUPPRESSED = "mcp_tasks_declaration_suppressed"
#: #1115: another live driver already holds the lease on this task, so this driver
#: refused to poll/answer it rather than double-prompt and double-update.
MCP_TASK_LEASE_HELD = "mcp_task_lease_held"
#: #1115: the durable write for a task record failed (its session row vanished, or
#: the registry rejected the update), so the record was moved to the process-local
#: holding path — still resumable/cancellable here, but not across a restart.
MCP_TASK_RECORD_HELD_LOCALLY = "mcp_task_record_held_locally"
#: #1115: the session owning live tasks was deleted. Deletion is never blocked; the
#: records are cancel-requested best-effort and migrated to the holding path.
MCP_TASK_SESSION_DELETED = "mcp_task_session_deleted"
#: #1115: a task exists on the server but its id could NOT be made durable. The
#: caller gets this reason plus the taskId and backend identity so the orphan can be
#: reconciled by hand (SEP-2663 has no tasks/list to rediscover it).
MCP_TASK_RECORD_NOT_DURABLE = "mcp_task_record_not_durable"
#: #1201: an execution-path client resolved ``tools.mcp.connect_mode=auto`` but
#: negotiated the LEGACY era anyway (the #1186 race: a slow first response burns
#: the modern probe and its one re-probe, so the client falls back to the
#: ``initialize`` handshake even though client and server both speak 2026-07-28).
#: Recorded ONLY under auto mode; a pinned mode (an explicit version or the
#: literal ``"legacy"``) is operator intent and never emits this.
MCP_PROTOCOL_DOWNGRADED_TO_LEGACY = "mcp_protocol_downgraded_to_legacy"
#: #1201: an ``mcp.yaml`` declaration file exists but could not be read/parsed
#: (OS error or malformed YAML). Previously swallowed into ``{}`` -- silently
#: treating "malformed" the same as "no servers declared". A MISSING file is
#: still normal and returns ``{}`` without this reason.
MCP_YAML_DECLARATION_UNREADABLE = "mcp_yaml_declaration_unreadable"
#: #1232 pt 2: a declared MCP namespace did not answer its bounded discovery
#: attempt (connect + list_tools) before the per-namespace deadline. NEVER
#: blocks "agent ready" -- the namespace's tools are simply absent from the
#: catalog until a background re-probe heals it (MCP_NAMESPACE_DISCOVERY_HEALED).
MCP_NAMESPACE_DISCOVERY_TIMEOUT = "mcp_namespace_discovery_timeout"
#: #1232 pt 2: a declared MCP namespace's discovery attempt raised (spawn
#: failure, connection refused, malformed response, ...) rather than timing
#: out. Same non-blocking/heals-in-background contract as the timeout reason.
MCP_NAMESPACE_DISCOVERY_UNREACHABLE = "mcp_namespace_discovery_unreachable"
#: #1232 pt 2: a previously-degraded namespace (either reason above) answered
#: on a background re-probe; its tools are now merged into the live catalog.
MCP_NAMESPACE_DISCOVERY_HEALED = "mcp_namespace_discovery_healed"
#: #1232 pt 3: a stdio MCP launcher (uv/uvx) could not acquire the dedicated
#: launcher cache lock within the bounded wait -- fails FAST and typed instead
#: of hanging the connect forever (the #1186 cache-lock-contention family).
#: Feeds the same background re-probe as a discovery degrade.
LAUNCHER_CACHE_LOCK_TIMEOUT = "launcher_cache_lock_timeout"
#: #1232 pt 4: the boot process-census killed a PROVABLY-orphaned clio-launched
#: child process (dead parent PID + clio launcher identity) instead of only
#: reporting it. The shared clio-core CTE daemon is excluded by construction
#: (see runtime/process_census.py) and never matches this reason.
PROCESS_CENSUS_ORPHAN_REAPED = "process_census_orphan_reaped"


class ClioError(Exception):
    """Base error for all CLIO Agent errors.

    All CLIO errors serialize to a structured dict with error_type,
    message, and optional details. Raw tracebacks never reach users.

    Attributes:
        message: Human-readable error description
        error_type: Machine-readable error category
        details: Additional context (never raw tracebacks)
    """

    def __init__(
        self,
        message: str,
        error_type: str = "clio_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.error_type = error_type
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to structured JSON-compatible dict.

        Returns:
            Dict with error, message, and details keys.
        """
        return {
            "error": self.error_type,
            "message": self.message,
            "details": self.details,
        }


class ProviderError(ClioError):
    """LM provider unavailable, timeout, or authentication failure."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="provider_error", details=details)


class RoutingError(ClioError):
    """Router failed to classify query to an expert."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="routing_error", details=details)


class ExpertError(ClioError):
    """Expert execution failed (data, analysis, or visualization)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="expert_error", details=details)


class ToolError(ClioError):
    """MCP tool call failed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="tool_error", details=details)


class MCPProtocolError(ToolError):
    """Base for typed MCP JSON-RPC capability/protocol refusals."""

    def __init__(
        self,
        message: str,
        *,
        code: int,
        reason: str,
        error_type: str,
        protocol_data: Any = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.protocol_data = protocol_data
        ClioError.__init__(
            self,
            message,
            error_type=error_type,
            details={
                "reason": reason,
                "json_rpc_code": code,
                "protocol_data": protocol_data,
            },
        )


class MCPMissingRequiredClientCapabilityError(MCPProtocolError):
    """The MCP server refused a request because a required client capability is absent."""

    def __init__(self, message: str, protocol_data: Any = None) -> None:
        super().__init__(
            message,
            code=-32021,
            reason=MCP_CAPABILITY_REFUSED,
            error_type="mcp_missing_required_client_capability",
            protocol_data=protocol_data,
        )


class MCPUnsupportedProtocolVersionError(MCPProtocolError):
    """The MCP server refused the negotiated protocol version."""

    def __init__(self, message: str, protocol_data: Any = None) -> None:
        super().__init__(
            message,
            code=-32022,
            reason=MCP_PROTOCOL_REFUSED,
            error_type="mcp_unsupported_protocol_version",
            protocol_data=protocol_data,
        )


class MCPInputRequiredRoundsExceededError(ToolError):
    """The modern-era MRTR loop exceeded its round bound without terminating (#1114).

    A server kept returning ``InputRequiredResult`` past the config-resolved
    ``tools.mcp.input_required_max_rounds``. Surfaced by the executor as a TYPED
    degrade (``reason`` in the advertised x_clio_stream_fallback_reasons catalog)
    instead of the raw SDK ``InputRequiredRoundsExceededError``, so the model sees
    a typed, recoverable tool error rather than a traceback.
    """

    def __init__(self, max_rounds: int, tool: str = "") -> None:
        self.reason = MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED
        self.max_rounds = max_rounds
        super().__init__(
            f"MCP tool {tool!r} exceeded the input-required round bound ({max_rounds}): "
            "the server kept requesting input without completing. Raise "
            "tools.mcp.input_required_max_rounds or fix the server.",
            details={
                "reason": MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED,
                "max_rounds": max_rounds,
                "tool": tool,
            },
        )


class ConfigError(ClioError):
    """Configuration invalid or missing."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="config_error", details=details)


class CancellationError(ClioError):
    """User-requested cancellation observed by a cooperative execution path."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="cancelled", details=details)


def format_error_response(error: Exception) -> dict[str, Any]:
    """Format any exception as a structured JSON-compatible response.

    For ClioError subclasses, returns the structured to_dict() output.
    For all other exceptions, returns a generic internal_error response
    that never exposes raw tracebacks.

    Args:
        error: Any exception instance

    Returns:
        Structured error dict with error, message, and details keys.
    """
    if isinstance(error, ClioError):
        return error.to_dict()
    return {
        "error": "internal_error",
        "message": "An internal error occurred",
        "details": {},
    }
