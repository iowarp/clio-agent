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
