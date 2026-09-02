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

**#1275 / C1-S2 terminal-refusal side channel.** A protocol-refusal-class error
(-32021/-32022) is DETERMINISTIC: retrying it can never succeed. Left alone, a
tool-call boundary like ``dspy.predict.react_v2.ReActV2._execute_tool_calls``
(upstream, vendored) catches ANY tool exception — refusal included — and
converts it into a text observation the LM may keep re-triggering turn after
turn (the #1275 hang: 15+ minutes of an LM retrying a structurally-unresolvable
call). :func:`mark_terminal_refusal` is called from ONE chokepoint —
``gact/agents/reactv2.py``'s ``_mark_refusals_on_tool_calls``, which wraps
EVERY tool callable ``_RetainingReActV2.__init__`` registers (MCP-bridged,
instrumented native, dynamic-agent external, or a bare ``dspy.Tool`` a caller
builds by hand — every documented way to add a tool) right before an
exception is re-raised into dspy's swallow; it classifies the exception and,
if it IS a refusal, stashes it on a contextvar so
``_RetainingReActV2._execute_tool_calls`` (called on the SAME thread/task
immediately after the swallowing call returns) can
:func:`pop_pending_terminal_refusal` and re-raise the TYPED exception itself —
propagating it out of the loop instead of letting the LM decide whether a
deterministic refusal is worth retrying. This is the one narrow exception to
"the model is the router/decider" (CLAUDE.md superseding principle #1): a
protocol refusal is a structural fact, not a routing/completion judgment.
"""

from __future__ import annotations

import contextvars
from typing import cast

from mcp.client._input_required import InputRequiredRoundsExceededError
from mcp.shared.exceptions import MCPError

from clio_agent.errors import (
    MCPInputRequiredRoundsExceededError,
    MCPMissingRequiredClientCapabilityError,
    MCPProtocolError,
    MCPUnsupportedProtocolVersionError,
)

__all__ = [
    "mark_terminal_refusal",
    "pop_pending_terminal_refusal",
    "typed_mcp_call_error",
    "typed_mcp_protocol_error",
]


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


#: One pending terminal refusal per thread/asyncio-task context (#1275/#901
#: C1-S2) -- see the module docstring. ``None`` is the normal, empty state;
#: never leaks across concurrent children (contextvars are per-Task/per-thread).
_PENDING_TERMINAL_REFUSAL: "contextvars.ContextVar[MCPProtocolError | None]" = (
    contextvars.ContextVar("clio_mcp_pending_terminal_refusal", default=None)
)


def mark_terminal_refusal(error: BaseException) -> MCPProtocolError | None:
    """Classify ``error``; if it is a protocol refusal, stash it for the loop chokepoint.

    Call this at a tool-callable boundary immediately BEFORE re-raising an
    exception into a layer that might swallow it into an LM-visible text
    observation (never instead of re-raising -- this only records, the caller
    still raises unchanged). Idempotent-safe: the FIRST refusal recorded in a
    context wins (a later, non-refusal exception in the same context never
    clears it) so a batch of tool calls that raises a refusal followed by an
    unrelated error still surfaces the deterministic one.

    Args:
        error: The exception the tool callable is about to (re-)raise. May
            already be a typed :class:`~clio_agent.errors.MCPProtocolError`
            (the common case -- executor/direct call sites type it first) or a
            raw SDK error this function classifies itself.

    Returns:
        The typed :class:`~clio_agent.errors.MCPProtocolError`, or ``None``
        when ``error`` is not a protocol refusal (nothing is stashed).
    """

    typed = error if isinstance(error, MCPProtocolError) else typed_mcp_protocol_error(error)
    if typed is not None and _PENDING_TERMINAL_REFUSAL.get() is None:
        _PENDING_TERMINAL_REFUSAL.set(typed)
    return typed


def pop_pending_terminal_refusal() -> MCPProtocolError | None:
    """Consume (clear) the pending terminal refusal recorded by :func:`mark_terminal_refusal`.

    Called exactly once per tool-call step by the loop chokepoint
    (``gact/agents/reactv2.py``). Returns ``None`` when no refusal was
    recorded for this step -- the normal case -- and clears the slot either
    way so a stale refusal can never leak into the NEXT step.
    """

    pending = _PENDING_TERMINAL_REFUSAL.get()
    if pending is not None:
        _PENDING_TERMINAL_REFUSAL.set(None)
    return pending
