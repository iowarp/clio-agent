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
    └── ConfigError    -- Configuration invalid
"""

from __future__ import annotations

from typing import Any


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


class ConfigError(ClioError):
    """Configuration invalid or missing."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, error_type="config_error", details=details)


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
