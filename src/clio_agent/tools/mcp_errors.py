"""Typed translation of raw MCP client errors at every direct call boundary.

The ONE seam every ``client.call_tool`` path applies so a raw SDK exception never
escapes into a CLIO surface (the wire, the model, telemetry). Two families are
mapped today:

* **MRTR exhaustion** (#1114) — the SDK's ``InputRequiredRoundsExceededError``, raised
  when a modern-era server keeps returning ``InputRequiredResult`` past the
  config-resolved round bound, becomes
  :class:`~clio_agent.errors.MCPInputRequiredRoundsExceededError` carrying the
  advertised ``mcp_input_required_rounds_exceeded`` reason.
* **Protocol refusals** (#1112) — the JSON-RPC ``-32021`` / ``-32022`` refusal codes
  become their typed :class:`~clio_agent.errors.MCPProtocolError` subclasses, mapped
  by CODE only (never by inspecting message text).

Applied by :class:`~clio_agent.tools.mcp_executor.AsyncMCPToolExecutor` and by the
direct factory-built execution paths that bypass the executor: the per-call REST
route (``gact/routes/mcp.py``) and the dynamic-agent external tool call
(``gact/agents/builders.py``).
"""

from __future__ import annotations

from typing import cast

from mcp.client._input_required import InputRequiredRoundsExceededError
from mcp.shared.exceptions import MCPError

from clio_agent.errors import (
    MCPInputRequiredRoundsExceededError,
    MCPMissingRequiredClientCapabilityError,
    MCPProtocolError,
    MCPUnsupportedProtocolVersionError,
)

__all__ = ["typed_mcp_call_error", "typed_mcp_protocol_error"]


def typed_mcp_protocol_error(error: BaseException) -> MCPProtocolError | None:
    """Map supported MCP JSON-RPC refusal codes without inspecting message text."""

    if not isinstance(error, MCPError):
        return None
    mcp_error = cast(MCPError, error)
    if mcp_error.code == -32021:
        return MCPMissingRequiredClientCapabilityError(mcp_error.message, mcp_error.data)
    if mcp_error.code == -32022:
        return MCPUnsupportedProtocolVersionError(mcp_error.message, mcp_error.data)
    return None


def typed_mcp_call_error(error: BaseException, *, tool: str = "") -> Exception | None:
    """Return CLIO's typed equivalent of a raw MCP call error, or ``None``.

    ``None`` means the error has no typed mapping and the caller keeps its own
    handling (re-raise / wrap) unchanged. Callers apply this BEFORE recording
    telemetry or building a wire envelope, so neither the SDK exception class nor
    its message can leak into a CLIO surface.
    """

    if isinstance(error, InputRequiredRoundsExceededError):
        return MCPInputRequiredRoundsExceededError(getattr(error, "max_rounds", 0), tool=tool)
    return typed_mcp_protocol_error(error)
