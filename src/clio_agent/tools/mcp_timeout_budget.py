"""Derived per-call MCP timeout backstop (iowarp/clio-agent#1230).

``AsyncMCPToolExecutor._timeout_budget_for_call`` (``tools/mcp_executor.py``)
already lets a CALLER extend a call's budget by explicitly passing a
``timeout_seconds`` argument (``_explicit_tool_timeout_seconds``), and #1225
already made a declared ``wait_for_terminal=True`` call unbounded. What was
still missing: an ORDINARY call (no explicit caller-supplied budget, not a
wait_for_terminal commitment) fell straight to the executor's flat,
operator-tuned global (``CLIO_MCP_CALL_TIMEOUT_S`` / ``tools.mcp.call_timeout_s``)
even when the TOOL ITSELF, via its own MCP schema, already declares that it
typically needs longer — the live defect: a relay dispatch tool whose schema
declares a 600s ``timeout_seconds`` default died at a 180s/300s global because
nothing ever read that default. :func:`component_declared_timeout_seconds` is
that missing signal: a schema DEFAULT value is the component's own declared
expectation (never a caller's per-call override, which stays
``_explicit_tool_timeout_seconds``'s job), so the derived per-call backstop
becomes the max of the global and whatever a tool declares it needs — never
UNDER a component-declared budget, and identical to today's flat global for
any tool that declares nothing.

Scoped to ``timeout_seconds`` only (not ``wait_timeout_seconds``): the wait
budget only ever describes an ACTIVE ``wait_for_terminal=True`` call, and any
such call already takes #1225's unbounded path before this derivation ever
runs — so gating on it here would be dead code, and reading it unconditionally
would wrongly inflate an ordinary (non-waiting) call to this same tool name.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

#: The caller-override field ``_explicit_tool_timeout_seconds`` also reads
#: unconditionally (never gated on ``wait_for_terminal``) — its SCHEMA DEFAULT
#: is exactly what a tool declares it typically needs when the caller omits it.
_TIMEOUT_FIELD = "timeout_seconds"


def component_declared_timeout_seconds(properties: Mapping[str, Any]) -> float | None:
    """Return the tool's own declared timeout floor from its schema default.

    Args:
        properties: The MCP tool input schema's ``properties`` mapping (as
            already extracted by ``tools.mcp_executor._tool_input_schema``).

    Returns:
        The tool's schema-declared ``timeout_seconds`` default, if positive
        and finite, else ``None``.
    """

    prop = properties.get(_TIMEOUT_FIELD)
    if not isinstance(prop, Mapping):
        return None
    raw = prop.get("default")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None
